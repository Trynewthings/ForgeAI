from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.exceptions import AppException
from app.db.models import Executable
from app.schemas.executable_manifest import ParsedManifest


class ExecutableManifestParser:
    def parse(self, executable: Executable) -> ParsedManifest:
        manifest_payload = self._load_manifest_payload(executable)
        loaded_documents = self._load_referenced_documents(executable, manifest_payload)
        parsed = ParsedManifest.model_validate(
            {
                **manifest_payload,
                "loaded_documents": loaded_documents,
            }
        )
        return parsed

    def _load_manifest_payload(self, executable: Executable) -> dict[str, Any]:
        manifest_path = self._resolve_manifest_path(executable)
        if manifest_path is not None:
            return self._read_json_file(manifest_path)
        if executable.manifest_json:
            return dict(executable.manifest_json)
        raise AppException("Executable manifest not found", status_code=400)

    def _load_referenced_documents(self, executable: Executable, manifest_payload: dict[str, Any]) -> dict[str, Any]:
        loaded: dict[str, Any] = {}
        references = {
            "entrypoint": manifest_payload.get("entrypoint", {}).get("path"),
            "schemas.input": manifest_payload.get("schemas", {}).get("input"),
            "schemas.output": manifest_payload.get("schemas", {}).get("output"),
            "schemas.config": manifest_payload.get("schemas", {}).get("config"),
            "execution.steps_source": manifest_payload.get("execution", {}).get("steps_source"),
            "execution.nodes_source": manifest_payload.get("execution", {}).get("nodes_source"),
            "artifacts.spec_file": manifest_payload.get("artifacts", {}).get("spec_file"),
            "package.bindings": manifest_payload.get("package", {}).get("bindings"),
            "package.runtime_entry": manifest_payload.get("package", {}).get("runtime_entry"),
            "package.package_metadata": manifest_payload.get("package", {}).get("package_metadata"),
            "package.asset_index": manifest_payload.get("package", {}).get("asset_index"),
            "workflow_native.workflow_artifact": (
                manifest_payload.get("metadata", {})
                .get("workflow_native_sidecar", {})
                .get("workflow_artifact")
            ),
        }
        for key, relative_path in references.items():
            if not relative_path:
                continue
            file_path = self._resolve_named_file(executable, relative_path)
            if file_path is None or not file_path.exists():
                continue
            loaded[key] = self._read_file_by_suffix(file_path)
        return loaded

    def _resolve_manifest_path(self, executable: Executable) -> Path | None:
        if executable.source_path:
            manifest_path = Path(executable.source_path) / "manifest.json"
            if manifest_path.exists():
                return manifest_path
        return self._resolve_named_file(executable, "manifest.json")

    def _resolve_named_file(self, executable: Executable, name: str) -> Path | None:
        if executable.source_path:
            candidate = Path(executable.source_path) / name
            if candidate.exists():
                return candidate
        generated_files = (executable.generated_files_index_json or {}).get("files", [])
        for item in generated_files:
            if item.get("path") == name or item.get("filename") == name:
                file_path = item.get("file_path")
                if file_path:
                    candidate = Path(file_path)
                    if candidate.exists():
                        return candidate
        return None

    def _read_json_file(self, file_path: Path) -> dict[str, Any]:
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AppException(f"Invalid JSON in {file_path.name}: {exc}", status_code=400) from exc

    def _read_file_by_suffix(self, file_path: Path) -> Any:
        if file_path.suffix == ".json":
            return self._read_json_file(file_path)
        return file_path.read_text(encoding="utf-8")
