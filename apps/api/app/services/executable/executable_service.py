from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models import Executable
from app.db.repositories.blueprint_repository import BlueprintRepository
from app.db.repositories.executable_repository import ExecutableRepository
from app.db.repositories.project_repository import ProjectRepository
from app.db.repositories.runtime_profile_repository import RuntimeProfileRepository
from app.repositories.artifact_repository import ArtifactRepository
from app.schemas.artifact import ArtifactResponse
from app.schemas.executable_manifest import (
    ManifestValidationResult,
    ParsedManifest,
    ResolveRunPlanRequest,
    ResolvedExecutionPlan,
)
from app.schemas.executable import (
    ExecutableCreateRequest,
    ExecutableFileContentResponse,
    ExecutableFileItem,
    ExecutableListItem,
    ExecutableResponse,
)
from app.services.executable_manifest.parser import ExecutableManifestParser
from app.services.executable_manifest.resolver import ExecutableManifestResolver
from app.services.executable_manifest.validator import ExecutableManifestValidator


def _iso(value) -> str:
    return value.isoformat() if value else ""


class ExecutableService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.project_repository = ProjectRepository(db)
        self.blueprint_repository = BlueprintRepository(db)
        self.executable_repository = ExecutableRepository(db)
        self.runtime_profile_repository = RuntimeProfileRepository(db)
        self.artifact_repository = ArtifactRepository(db)
        self.manifest_parser = ExecutableManifestParser()
        self.manifest_validator = ExecutableManifestValidator()
        self.manifest_resolver = ExecutableManifestResolver()

    def create_executable(self, payload: ExecutableCreateRequest) -> ExecutableResponse:
        if self.project_repository.get_by_id(payload.project_id) is None:
            raise AppException("Project not found", status_code=404)
        blueprint = self.blueprint_repository.get_by_id(payload.blueprint_id)
        if blueprint is None or blueprint.project_id != payload.project_id:
            raise AppException("Blueprint not found for this project", status_code=404)
        executable = Executable(**self._normalized_payload(payload.model_dump(mode="python", exclude_none=True)))
        self.executable_repository.create(executable)
        self.db.commit()
        return self._serialize(executable)

    def get_executable(self, executable_id: str) -> ExecutableResponse:
        executable = self.executable_repository.get_by_id(executable_id)
        if executable is None:
            raise AppException("Executable not found", status_code=404)
        return self._serialize(executable)

    def list_project_executables(self, project_id: str) -> list[ExecutableListItem]:
        if self.project_repository.get_by_id(project_id) is None:
            raise AppException("Project not found", status_code=404)
        return [
            ExecutableListItem(
                id=item.id,
                blueprint_id=item.blueprint_id,
                executable_type=item.executable_type,
                build_version=item.build_version,
                status=item.status,
                created_at=_iso(item.created_at),
                updated_at=_iso(item.updated_at),
            )
            for item in self.executable_repository.list_by_project(project_id)
        ]

    def get_executable_artifacts(self, executable_id: str) -> list[ArtifactResponse]:
        executable = self.executable_repository.get_by_id(executable_id)
        if executable is None:
            raise AppException("Executable not found", status_code=404)
        return [
            ArtifactResponse(
                id=item.id,
                build_id=item.build_id,
                build_step_id=item.build_step_v2_id,
                run_id=item.run_id,
                run_step_id=item.run_step_id,
                executable_id=item.executable_id,
                artifact_type=item.artifact_type,
                storage_kind=item.storage_kind,
                path_or_content=item.path_or_content,
                metadata_json=item.metadata_json or {},
                created_at=_iso(item.created_at),
            )
            for item in self.artifact_repository.list_by_executable(executable_id)
        ]

    def list_executable_files(self, executable_id: str) -> list[ExecutableFileItem]:
        executable = self._get_executable_or_404(executable_id)
        entries = self._build_file_entries(executable)
        return [
            ExecutableFileItem(
                path=item["path"],
                kind=item["kind"],
                content_type=item["content_type"],
                exists=item["exists"],
                size_bytes=item.get("size_bytes"),
            )
            for item in entries
        ]

    def get_executable_file_content(self, executable_id: str, path: str) -> ExecutableFileContentResponse:
        executable = self._get_executable_or_404(executable_id)
        normalized_path = self._normalize_relative_path(path)
        if not normalized_path:
            raise AppException("File path is required", status_code=400)

        entries = {item["path"]: item for item in self._build_file_entries(executable)}
        entry = entries.get(normalized_path)
        if entry is None:
            raise AppException("Executable file not found", status_code=404)

        content = self._resolve_file_content(entry)
        if content is None:
            raise AppException("Executable file content is not available", status_code=404)

        return ExecutableFileContentResponse(
            path=entry["path"],
            kind=entry["kind"],
            content_type=entry["content_type"],
            formatted_hint=entry["formatted_hint"],
            exists=entry["exists"],
            content=content,
        )

    def parse_manifest(self, executable_id: str) -> ParsedManifest:
        executable = self._get_executable_or_404(executable_id)
        return self.manifest_parser.parse(executable)

    def validate_manifest(self, executable_id: str) -> ManifestValidationResult:
        return self.manifest_validator.validate(self.parse_manifest(executable_id))

    def resolve_run_plan(self, executable_id: str, payload: ResolveRunPlanRequest) -> ResolvedExecutionPlan:
        executable = self._get_executable_or_404(executable_id)
        manifest = self.manifest_parser.parse(executable)
        validation = self.manifest_validator.validate(manifest)
        if not validation.valid:
            raise AppException("; ".join(validation.errors), status_code=400)
        runtime_profile = None
        runtime_profile_id = (payload.runtime_profile_id or "").strip()
        if runtime_profile_id:
            runtime_profile = self.runtime_profile_repository.get_by_id(runtime_profile_id)
            if runtime_profile is None:
                raise AppException(f"Runtime profile not found: {runtime_profile_id}", status_code=404)
            if runtime_profile.project_id != executable.project_id:
                raise AppException("Runtime profile does not belong to the executable project", status_code=400)
            if runtime_profile.blueprint_id and runtime_profile.blueprint_id != executable.blueprint_id:
                raise AppException("Runtime profile does not match the executable blueprint", status_code=400)
        else:
            runtime_profile = (
                self.runtime_profile_repository.get_default_by_blueprint(executable.blueprint_id)
                or self.runtime_profile_repository.get_default_by_project(executable.project_id)
            )
        plan = self.manifest_resolver.resolve(
            executable=executable,
            manifest=manifest,
            runtime_profile=runtime_profile,
            run_input=payload.run_input,
        )
        if not plan.preflight.valid:
            raise AppException(plan.preflight.failure_detail or "; ".join(plan.preflight.errors), status_code=400)
        return plan

    def _serialize(self, executable: Executable) -> ExecutableResponse:
        return ExecutableResponse(
            id=executable.id,
            project_id=executable.project_id,
            blueprint_id=executable.blueprint_id,
            executable_type=executable.executable_type,
            source_path=executable.source_path,
            manifest_json=executable.manifest_json or {},
            generated_files_index_json=executable.generated_files_index_json or {},
            build_version=executable.build_version,
            status=executable.status,
            created_at=_iso(executable.created_at),
            updated_at=_iso(executable.updated_at),
        )

    def _get_executable_or_404(self, executable_id: str) -> Executable:
        executable = self.executable_repository.get_by_id(executable_id)
        if executable is None:
            raise AppException("Executable not found", status_code=404)
        return executable

    def _build_file_entries(self, executable: Executable) -> list[dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        artifact_map = self._artifact_content_map(executable.id)

        def register_entry(
            relative_path: str,
            kind: str,
            *,
            disk_path: str | None = None,
            virtual_payload: Any | None = None,
        ) -> None:
            normalized_path = self._normalize_relative_path(relative_path)
            if not normalized_path:
                return
            candidate_path = self._resolve_disk_path(executable, normalized_path, disk_path)
            artifact_content = artifact_map.get(normalized_path)
            content_type = self._content_type_for_path(normalized_path)
            formatted_hint = self._formatted_hint_for_content_type(content_type)
            size_bytes = None
            exists = False
            if virtual_payload is not None:
                exists = True
                size_bytes = len(self._serialize_content(virtual_payload).encode("utf-8"))
            elif candidate_path is not None and candidate_path.exists():
                exists = True
                size_bytes = candidate_path.stat().st_size
            elif artifact_content is not None:
                exists = True
                size_bytes = len(artifact_content.encode("utf-8"))

            existing = entries.get(normalized_path)
            entry = {
                "path": normalized_path,
                "kind": kind,
                "content_type": content_type,
                "formatted_hint": formatted_hint,
                "exists": exists,
                "size_bytes": size_bytes,
                "disk_path": str(candidate_path) if candidate_path is not None else None,
                "virtual_payload": virtual_payload,
                "artifact_content": artifact_content,
            }
            if existing is None or (not existing["exists"] and entry["exists"]):
                entries[normalized_path] = entry

        register_entry("manifest.json", "manifest", virtual_payload=executable.manifest_json or {})
        register_entry("generated_files_index.json", "generated_files_index", virtual_payload=executable.generated_files_index_json or {})

        manifest_files = (executable.manifest_json or {}).get("files", [])
        for item in manifest_files:
            if not isinstance(item, dict):
                continue
            register_entry(str(item.get("path") or ""), str(item.get("kind") or "generated"))

        generated_files = (executable.generated_files_index_json or {}).get("files", [])
        for item in generated_files:
            if not isinstance(item, dict):
                continue
            register_entry(
                str(item.get("filename") or item.get("path") or ""),
                str(item.get("artifact_type") or item.get("kind") or "generated_file"),
                disk_path=item.get("file_path"),
            )

        return sorted(entries.values(), key=lambda item: (item["path"] != "manifest.json", item["path"]))

    def _artifact_content_map(self, executable_id: str) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for artifact in self.artifact_repository.list_by_executable(executable_id):
            metadata = artifact.metadata_json or {}
            file_name = metadata.get("filename") or metadata.get("path")
            if isinstance(file_name, str):
                normalized = self._normalize_relative_path(file_name)
                if normalized and normalized not in mapping:
                    mapping[normalized] = artifact.path_or_content
        return mapping

    def _resolve_disk_path(self, executable: Executable, relative_path: str, explicit_path: str | None) -> Path | None:
        if explicit_path:
            candidate = Path(explicit_path)
            if candidate.exists():
                return candidate
        if executable.source_path:
            base_path = Path(executable.source_path)
            candidate = (base_path / relative_path).resolve()
            try:
                candidate.relative_to(base_path.resolve())
            except ValueError:
                return None
            return candidate
        return None

    def _resolve_file_content(self, entry: dict[str, Any]) -> str | None:
        if entry.get("virtual_payload") is not None:
            return self._serialize_content(entry["virtual_payload"])
        disk_path = entry.get("disk_path")
        if disk_path:
            candidate = Path(disk_path)
            if candidate.exists():
                return candidate.read_text(encoding="utf-8")
        artifact_content = entry.get("artifact_content")
        if isinstance(artifact_content, str):
            return artifact_content
        return None

    def _serialize_content(self, payload: Any) -> str:
        if isinstance(payload, (dict, list)):
            return json.dumps(payload, ensure_ascii=False, indent=2)
        return str(payload)

    def _normalize_relative_path(self, value: str) -> str:
        normalized = value.replace("\\", "/").strip()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lstrip("/")
        if not normalized or ".." in Path(normalized).parts:
            return ""
        return normalized

    def _content_type_for_path(self, path: str) -> str:
        lowered = path.lower()
        if lowered.endswith(".json"):
            return "application/json"
        if lowered.endswith(".md"):
            return "text/markdown"
        return "text/plain"

    def _formatted_hint_for_content_type(self, content_type: str) -> str:
        if content_type == "application/json":
            return "json"
        if content_type == "text/markdown":
            return "markdown"
        return "text"

    def _normalized_payload(self, payload: dict) -> dict:
        for field_name, value in list(payload.items()):
            if hasattr(value, "value"):
                payload[field_name] = value.value
        return payload
