from __future__ import annotations

from typing import Any

from app.schemas.executable_manifest import (
    ExecutionPackageAssetIndex,
    ExecutionPackageBindingsDocument,
    ExecutionPackageMetadata,
    ExecutionPackageRuntimeEntry,
    ManifestValidationResult,
    ParsedManifest,
    RuntimeInputValidationResult,
)


class ExecutableManifestValidator:
    SUPPORTED_TYPES = {"single_service", "workflow", "multi_agent"}
    SUPPORTED_STRATEGIES = {"sequential", "typed_node_v0"}
    SUPPORTED_ENTRYPOINTS = {"steps_file", "service_descriptor", "agent_graph"}

    def validate(self, manifest: ParsedManifest) -> ManifestValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        if not manifest.manifest_version:
            errors.append("manifest_version is required")

        if manifest.executable_type not in self.SUPPORTED_TYPES:
            errors.append("executable_type must be one of: single_service, workflow, multi_agent")

        if manifest.entrypoint.kind not in self.SUPPORTED_ENTRYPOINTS:
            errors.append("entrypoint.kind is not supported")
        if not manifest.entrypoint.path:
            errors.append("entrypoint.path is required")
        elif "entrypoint" not in manifest.loaded_documents:
            errors.append("entrypoint.path could not be resolved")

        if manifest.execution.strategy not in self.SUPPORTED_STRATEGIES:
            errors.append("execution.strategy must be one of: sequential, typed_node_v0")

        if manifest.execution.steps_source:
            if "execution.steps_source" not in manifest.loaded_documents:
                errors.append("execution.steps_source could not be resolved")
        else:
            warnings.append("execution.steps_source is empty; runtime will rely on entrypoint only")

        if manifest.execution.nodes_source and "execution.nodes_source" not in manifest.loaded_documents:
            errors.append("execution.nodes_source could not be resolved")

        for schema_field in ("input", "output", "config"):
            schema_path = getattr(manifest.schemas, schema_field)
            if schema_path and f"schemas.{schema_field}" not in manifest.loaded_documents:
                errors.append(f"schemas.{schema_field} could not be resolved")

        if not isinstance(manifest.runtime.env_required, list) or any(
            not isinstance(item, str) or not item.strip() for item in manifest.runtime.env_required
        ):
            errors.append("runtime.env_required must be a list of non-empty strings")

        if manifest.artifacts.spec_file and "artifacts.spec_file" not in manifest.loaded_documents:
            errors.append("artifacts.spec_file could not be resolved")

        package = manifest.package
        if package.bindings:
            if "package.bindings" not in manifest.loaded_documents:
                errors.append("package.bindings could not be resolved")
            else:
                self._validate_package_document(
                    "package.bindings",
                    manifest.loaded_documents["package.bindings"],
                    ExecutionPackageBindingsDocument,
                    errors,
                )
        if package.runtime_entry:
            if "package.runtime_entry" not in manifest.loaded_documents:
                errors.append("package.runtime_entry could not be resolved")
            else:
                self._validate_package_document(
                    "package.runtime_entry",
                    manifest.loaded_documents["package.runtime_entry"],
                    ExecutionPackageRuntimeEntry,
                    errors,
                )
        if package.package_metadata:
            if "package.package_metadata" not in manifest.loaded_documents:
                errors.append("package.package_metadata could not be resolved")
            else:
                self._validate_package_document(
                    "package.package_metadata",
                    manifest.loaded_documents["package.package_metadata"],
                    ExecutionPackageMetadata,
                    errors,
                )
        if package.asset_index:
            if "package.asset_index" not in manifest.loaded_documents:
                errors.append("package.asset_index could not be resolved")
            else:
                self._validate_package_document(
                    "package.asset_index",
                    manifest.loaded_documents["package.asset_index"],
                    ExecutionPackageAssetIndex,
                    errors,
                )

        steps_document = manifest.loaded_documents.get("execution.steps_source")
        if steps_document is not None and not isinstance(steps_document, dict):
            errors.append("execution.steps_source must resolve to a JSON object")
        if isinstance(steps_document, dict):
            steps = steps_document.get("steps", [])
            if not isinstance(steps, list):
                errors.append("execution.steps_source.steps must be a list")

        return ManifestValidationResult(valid=len(errors) == 0, errors=errors, warnings=warnings)

    def _validate_package_document(self, field_name: str, payload: Any, schema_model, errors: list[str]) -> None:  # noqa: ANN001
        if not isinstance(payload, dict):
            errors.append(f"{field_name} must resolve to a JSON object")
            return
        try:
            schema_model.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{field_name} is invalid: {exc}")

    def validate_runtime_input(
        self,
        *,
        execution_mode: str,
        manifest: ParsedManifest | None,
        resolved_input: Any,
        field_sources: dict[str, str] | None = None,
    ) -> RuntimeInputValidationResult:
        if execution_mode not in {"executable-manifest", "execution-package", "typed-node-v0"}:
            return RuntimeInputValidationResult(
                valid=False,
                contract_mode="manifest_input_schema",
                schema_kind="unsupported",
                errors=[f"Unsupported execution mode: {execution_mode}"],
                field_sources=field_sources or {},
            )

        if not isinstance(resolved_input, dict):
            return RuntimeInputValidationResult(
                valid=False,
                contract_mode="manifest_input_schema",
                schema_kind="json_schema_subset",
                errors=["Resolved runtime input must be a JSON object."],
                field_sources=field_sources or {},
            )

        schema_document = {}
        if manifest is not None:
            loaded = manifest.loaded_documents.get("schemas.input")
            if isinstance(loaded, dict):
                schema_document = loaded

        if not schema_document:
            return RuntimeInputValidationResult(
                valid=True,
                contract_mode="manifest_input_schema",
                schema_kind="none",
                warnings=["No executable input schema was available; runtime input used object-only validation."],
                field_sources=field_sources or {},
            )

        errors: list[str] = []
        warnings: list[str] = []
        self._validate_schema_node(
            value=resolved_input,
            schema=schema_document,
            path="input",
            errors=errors,
            warnings=warnings,
        )
        return RuntimeInputValidationResult(
            valid=len(errors) == 0,
            contract_mode="manifest_input_schema",
            schema_kind="json_schema_subset",
            errors=errors,
            warnings=warnings,
            field_sources=field_sources or {},
        )

    def _validate_schema_node(
        self,
        *,
        value: Any,
        schema: dict[str, Any],
        path: str,
        errors: list[str],
        warnings: list[str],
    ) -> None:
        schema_type = schema.get("type")
        if schema_type:
            if not self._matches_type(value, schema_type):
                errors.append(f"{path} must be of type {schema_type}")
                return

        if schema_type == "object" and isinstance(value, dict):
            required_fields = schema.get("required", [])
            if isinstance(required_fields, list):
                for key in required_fields:
                    if key not in value:
                        errors.append(f"{path}.{key} is required")

            properties = schema.get("properties", {})
            if isinstance(properties, dict):
                for key, property_schema in properties.items():
                    if key not in value or not isinstance(property_schema, dict):
                        continue
                    self._validate_schema_node(
                        value=value[key],
                        schema=property_schema,
                        path=f"{path}.{key}",
                        errors=errors,
                        warnings=warnings,
                    )

            additional_properties = schema.get("additionalProperties", True)
            if additional_properties is False and isinstance(properties, dict):
                allowed = set(properties.keys())
                extras = sorted(key for key in value.keys() if key not in allowed)
                for key in extras:
                    errors.append(f"{path}.{key} is not allowed")

        if schema_type == "array" and isinstance(value, list):
            item_schema = schema.get("items")
            if isinstance(item_schema, dict):
                for index, item in enumerate(value):
                    self._validate_schema_node(
                        value=item,
                        schema=item_schema,
                        path=f"{path}[{index}]",
                        errors=errors,
                        warnings=warnings,
                    )

    def _matches_type(self, value: Any, schema_type: str) -> bool:
        if schema_type == "object":
            return isinstance(value, dict)
        if schema_type == "array":
            return isinstance(value, list)
        if schema_type == "string":
            return isinstance(value, str)
        if schema_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if schema_type == "number":
            return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(value, float)
        if schema_type == "boolean":
            return isinstance(value, bool)
        if schema_type == "null":
            return value is None
        return True
