from app.services.capabilities import CapabilityRegistryService


def test_capability_registry_snapshot_loads_static_taxonomy() -> None:
    service = CapabilityRegistryService()

    snapshot = service.snapshot(include_disabled=True)

    assert len(snapshot.logical_step_kinds) >= 8
    assert len(snapshot.capabilities) >= 10
    assert any(item.step_kind == "ingest" for item in snapshot.logical_step_kinds)
    assert any(item.capability_id == "write_artifact" for item in snapshot.capabilities)
    assert any(item.step_kind == "review" for item in snapshot.bindings)
    assert any(item.capability_id == "write_artifact" for item in snapshot.planner_capability_projections)


def test_capability_registry_snapshot_exposes_planner_capability_projection() -> None:
    service = CapabilityRegistryService()

    projection = service.build_planner_capability_projection("write_artifact")

    assert projection.capability_id == "write_artifact"
    assert "act" in projection.node_semantic_types
    assert "workflow_build" in projection.admissible_execution_regimes
    assert projection.side_effect_class in {"filesystem_write", "external_write"}


def test_capability_lookup_by_step_kind_and_context_filters_disabled_entries() -> None:
    service = CapabilityRegistryService()

    ingest_runtime = service.list_capabilities_for_step_kind("ingest", context="runtime")

    capability_ids = [item.capability_id for item in ingest_runtime]
    assert "fetch_url" in capability_ids
    assert "web_search" in capability_ids
    assert "filesystem_list" in capability_ids
    assert "filesystem_read" in capability_ids
    assert "browser_interaction" in capability_ids
    assert "rss_fetch" not in capability_ids


def test_capability_usage_validation_reports_disallowed_context() -> None:
    service = CapabilityRegistryService()

    result = service.validate_capability_usage(
        capability_id="send_email",
        context="authoring",
        step_kind="notify",
    )

    assert result.valid is False
    assert "Capability is not allowed in context 'authoring'." in result.errors


def test_capability_usage_validation_reports_disabled_capability() -> None:
    service = CapabilityRegistryService()

    result = service.validate_capability_usage(
        capability_id="rss_fetch",
        context="runtime",
        step_kind="ingest",
    )

    assert result.valid is False
    assert "Capability is registered but currently disabled." in result.errors


def test_capability_gap_is_reported_when_context_has_no_allowed_binding() -> None:
    service = CapabilityRegistryService()

    gap = service.detect_capability_gap(step_kind="notify", context="authoring")

    assert gap is not None
    assert gap.reason == "no_capability_allowed_in_context"
    assert gap.candidate_capability_ids == ["send_email"]


def test_capability_gap_is_none_when_context_has_allowed_capability() -> None:
    service = CapabilityRegistryService()

    gap = service.detect_capability_gap(step_kind="persist", context="runtime")

    assert gap is None


def test_capability_registry_suggests_registry_bound_alternatives() -> None:
    service = CapabilityRegistryService()

    alternatives = service.suggest_alternative_capabilities(
        step_kind="persist",
        context="build_planning",
        exclude_capability_ids=["run_python_template"],
    )

    capability_ids = [item.capability_id for item in alternatives]
    assert capability_ids
    assert "architecture_report_render" in capability_ids


def test_capability_gap_request_suggestion_is_emitted_when_no_alternative_exists() -> None:
    service = CapabilityRegistryService()

    gap = service.build_gap_response(
        gap_kind="unsupported_mention",
        stage="authoring",
        requested_capability_id_or_name="slack_connector",
        reason="Capability mention is not present in the registered authoring/runtime vocabulary.",
        review_required=True,
    )

    assert gap.alternative_capabilities == []
    assert gap.suggested_request is not None
    assert gap.suggested_request.request_kind == "manual_review"
