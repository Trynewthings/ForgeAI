from __future__ import annotations

import csv
import fnmatch
import json
from copy import deepcopy
from html import escape as html_escape
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

from app.core.config import ROOT_DIR


NEWS_ARTICLES: list[dict[str, Any]] = [
    {
        "id": "news-001",
        "title": "Open-weight coding model reaches stronger repo planning accuracy",
        "source": "Model Systems Journal",
        "published_at": "2026-03-10",
        "url": "https://example.com/news/open-weight-coding-model",
        "highlights": [
            "The model improved repository planning benchmarks by 18%.",
            "Teams reported lower prompt iteration overhead in scaffold generation.",
        ],
    },
    {
        "id": "news-002",
        "title": "Enterprise teams standardize AI workload observability dashboards",
        "source": "Infra Weekly",
        "published_at": "2026-03-11",
        "url": "https://example.com/news/ai-observability-dashboards",
        "highlights": [
            "Token and latency dashboards are now part of release readiness gates.",
            "Shared tracing formats reduce debugging time across product and platform teams.",
        ],
    },
    {
        "id": "news-003",
        "title": "Workflow-native copilots reduce handoff cost in research operations",
        "source": "Applied AI Review",
        "published_at": "2026-03-12",
        "url": "https://example.com/news/workflow-native-copilots",
        "highlights": [
            "Structured workflow steps made weekly analyst reviews more repeatable.",
            "Human reviewers kept final approval while automation handled first-pass summaries.",
        ],
    },
    {
        "id": "news-004",
        "title": "Local inference clusters become viable for weekly internal intelligence reports",
        "source": "Compute Ledger",
        "published_at": "2026-03-13",
        "url": "https://example.com/news/local-inference-clusters",
        "highlights": [
            "Cost per report dropped after moving triage steps to local inference.",
            "Teams still routed final narrative generation to hosted frontier models.",
        ],
    },
    {
        "id": "news-005",
        "title": "Governance-first AI delivery playbooks spread across enterprise PMOs",
        "source": "Enterprise Delivery Times",
        "published_at": "2026-03-13",
        "url": "https://example.com/news/governance-ai-playbooks",
        "highlights": [
            "Blueprint-driven delivery improved auditability for AI-enabled products.",
            "Release managers preferred file-based artifacts over chat-only outputs.",
        ],
    },
]


RESEARCH_ITEMS: list[dict[str, Any]] = [
    {
        "id": "research-001",
        "title": "Agent planning systems need stronger state visibility",
        "kind": "paper",
        "source": "Lab Notes",
        "published_at": "2026-03-01",
        "url": "https://example.com/research/state-visibility",
        "abstract": "State visibility is the main differentiator between reliable and brittle orchestration systems.",
        "tags": ["agent-systems", "observability", "planning"],
    },
    {
        "id": "research-002",
        "title": "Weekly research ops benefit from normalized evidence bundles",
        "kind": "blog",
        "source": "Ops Research Weekly",
        "published_at": "2026-03-05",
        "url": "https://example.com/research/evidence-bundles",
        "abstract": "Evidence bundles let reviewers compare sources and summaries without re-reading every document.",
        "tags": ["workflow", "research", "knowledge-ops"],
    },
    {
        "id": "research-003",
        "title": "Hosted small models are sufficient for relevance filtering",
        "kind": "report",
        "source": "Inference Monitor",
        "published_at": "2026-03-06",
        "url": "https://example.com/research/relevance-filtering",
        "abstract": "A compact hosted model handled relevance triage while larger models focused on synthesis.",
        "tags": ["llm-ops", "routing", "cost-control"],
    },
    {
        "id": "research-004",
        "title": "Blueprint manifests improve reproducibility in runtime execution",
        "kind": "paper",
        "source": "Systems Archive",
        "published_at": "2026-03-08",
        "url": "https://example.com/research/manifest-reproducibility",
        "abstract": "Manifest-driven execution reduced hidden assumptions during replay and incident debugging.",
        "tags": ["blueprint", "runtime", "replay"],
    },
]


TERMINAL_COMMAND_ALLOWLIST = {
    "pwd",
    "ls",
    "cat",
    "echo",
    "grep",
    "rg",
    "wc",
    "head",
    "tail",
    "find",
    "sed",
    "sort",
    "cut",
    "tr",
    "uniq",
}

REPOSITORY_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".md": "markdown",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".sh": "shell",
}

DEFAULT_REPOSITORY_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".next",
    ".turbo",
    "coverage",
}

REPOSITORY_CONFIG_FILES = {
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "Makefile",
    ".env",
    ".env.example",
    "tsconfig.json",
}

REPOSITORY_ENTRYPOINT_FILES = {
    "main.py",
    "app.py",
    "server.py",
    "manage.py",
    "cli.py",
    "index.ts",
    "index.js",
    "main.ts",
    "main.js",
    "wsgi.py",
    "asgi.py",
}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._current_href: str | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "a" and attr_map.get("href"):
            self._current_href = attr_map["href"]

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "a":
            self._current_href = None

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if not value:
            return
        self.text_parts.append(value)
        if self._in_title:
            self.title_parts.append(value)
        if self._current_href:
            self.links.append({"href": self._current_href, "text": value})


class _BoundedBrowserDocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[dict[str, Any]] = []
        self.forms: list[dict[str, Any]] = []
        self._current_href: str | None = None
        self._current_link: dict[str, Any] | None = None
        self._current_form: dict[str, Any] | None = None
        self._current_select: dict[str, Any] | None = None
        self._current_option: dict[str, Any] | None = None
        self._current_button: dict[str, Any] | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "a" and attr_map.get("href"):
            self._current_href = attr_map["href"]
            self._current_link = {
                "href": attr_map["href"],
                "text": "",
                "id": attr_map.get("id") or "",
                "name": attr_map.get("name") or "",
            }
        if tag == "form":
            self._current_form = {
                "id": attr_map.get("id") or "",
                "name": attr_map.get("name") or "",
                "action": attr_map.get("action") or "",
                "method": (attr_map.get("method") or "get").lower(),
                "inputs": [],
                "selects": [],
                "buttons": [],
            }
            self.forms.append(self._current_form)
        if tag == "input" and self._current_form is not None:
            self._current_form["inputs"].append(
                {
                    "type": (attr_map.get("type") or "text").lower(),
                    "name": attr_map.get("name") or "",
                    "id": attr_map.get("id") or "",
                    "value": attr_map.get("value") or "",
                }
            )
        if tag == "select" and self._current_form is not None:
            self._current_select = {
                "name": attr_map.get("name") or "",
                "id": attr_map.get("id") or "",
                "options": [],
            }
            self._current_form["selects"].append(self._current_select)
        if tag == "option" and self._current_select is not None:
            self._current_option = {
                "value": attr_map.get("value") or "",
                "label": "",
                "selected": "selected" in attr_map,
            }
            self._current_select["options"].append(self._current_option)
        if tag == "button" and self._current_form is not None:
            self._current_button = {
                "type": (attr_map.get("type") or "submit").lower(),
                "name": attr_map.get("name") or "",
                "id": attr_map.get("id") or "",
                "text": "",
            }
            self._current_form["buttons"].append(self._current_button)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag == "a":
            self._current_href = None
            if self._current_link is not None:
                self.links.append(self._current_link)
            self._current_link = None
        if tag == "button":
            self._current_button = None
        if tag == "option":
            self._current_option = None
        if tag == "select":
            self._current_select = None
        if tag == "form":
            self._current_form = None

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if not value:
            return
        self.text_parts.append(value)
        if self._in_title:
            self.title_parts.append(value)
        if self._current_link is not None:
            self._current_link["text"] = f"{self._current_link['text']} {value}".strip()
        if self._current_option is not None:
            self._current_option["label"] = f"{self._current_option['label']} {value}".strip()
        if self._current_button is not None:
            self._current_button["text"] = f"{self._current_button['text']} {value}".strip()


def _resolve_allowed_root(args: dict[str, Any]) -> Path:
    requested_root = str(args.get("allowed_root") or ROOT_DIR)
    root = Path(requested_root)
    if not root.is_absolute():
        root = (ROOT_DIR / root).resolve()
    else:
        root = root.resolve()
    if root != ROOT_DIR and ROOT_DIR not in root.parents:
        raise PermissionError(f"Allowed root '{root}' must remain inside the workspace.")
    return root


def _resolve_workspace_path(*, allowed_root: Path, raw_path: str | None) -> Path:
    target = Path(raw_path or ".")
    candidate = (allowed_root / target).resolve() if not target.is_absolute() else target.resolve()
    if candidate != allowed_root and allowed_root not in candidate.parents:
        raise PermissionError(f"Path '{candidate}' is outside the allowed workspace root.")
    return candidate


def _truncate_text(value: str, limit: int = 8000) -> str:
    return value if len(value) <= limit else f"{value[:limit]}\n...[truncated]"


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _resolve_structured_value(
    *,
    args: dict[str, Any],
    run_input: dict[str, Any],
    previous_outputs_by_step: dict[str, dict[str, Any]],
    default_field: str,
) -> Any:
    source_step = str(args.get("source_step") or "")
    source_field = str(args.get("source_field") or default_field)
    if source_step:
        structured = previous_outputs_by_step.get(source_step, {}).get("structured_output", {})
        if isinstance(structured, dict):
            return structured.get(source_field)
        return None
    return run_input.get(source_field)


def _resolve_repository_source_output(
    *,
    args: dict[str, Any],
    run_input: dict[str, Any],
    previous_outputs_by_step: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    candidate_step_names: list[str] = []
    for key in (
        "source_step",
        "inventory_step",
        "structure_step",
        "relationship_step",
        "execution_surface_step",
        "brief_step",
        "report_step",
    ):
        step_name = str(args.get(key) or "").strip()
        if step_name and step_name not in candidate_step_names:
            candidate_step_names.append(step_name)
    for step_name in candidate_step_names:
        structured = previous_outputs_by_step.get(step_name, {}).get("structured_output", {})
        if isinstance(structured, dict) and any(
            structured.get(field_name)
            for field_name in ("workspace_root", "repo_root", "source_locator")
        ):
            return structured
    return {}


def _repository_locator_from_args(
    *,
    args: dict[str, Any],
    run_input: dict[str, Any],
    source_output: dict[str, Any] | None = None,
) -> str:
    return str(
        (source_output or {}).get("source_locator")
        or args.get("repo_locator")
        or args.get("source_locator")
        or args.get("repo_url")
        or run_input.get("repo_locator")
        or run_input.get("source_locator")
        or run_input.get("repo_url")
        or run_input.get("repo_root")
        or args.get("repo_root")
        or ""
    ).strip()


def _repository_source_kind(locator: str) -> str:
    lowered = locator.lower()
    if not lowered:
        return "ambiguous_source"
    if lowered.startswith("svn://"):
        return "unsupported_source"
    if lowered.endswith((".zip", ".tar", ".tar.gz", ".tgz")):
        return "archive_url"
    if "github.com/" in lowered:
        return "github_repo_url"
    if lowered.startswith(("http://", "https://", "git://", "ssh://", "git@")) or lowered.endswith(".git") or lowered.startswith("file://"):
        return "git_remote"
    return "local_workspace_repo"


def _repository_checkout_slug(locator: str, source_kind: str) -> str:
    parsed = urlparse(locator)
    raw = ""
    if parsed.scheme == "file":
        raw = Path(unquote(parsed.path)).stem
    elif parsed.scheme:
        raw = Path(parsed.path).stem
    else:
        raw = Path(locator).stem
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", raw or source_kind).strip("-._")
    return normalized or source_kind


def _repository_checkout_destination(
    *,
    allowed_root: Path,
    args: dict[str, Any],
    run_input: dict[str, Any],
    locator: str,
    source_kind: str,
) -> Path:
    requested = str(
        args.get("destination_root")
        or args.get("checkout_destination")
        or run_input.get("checkout_destination")
        or f".generated/runtime_v592/repository_sources/{_repository_checkout_slug(locator, source_kind)}"
    )
    return _resolve_workspace_path(allowed_root=allowed_root, raw_path=requested)


def _materialize_repository_source(
    *,
    allowed_root: Path,
    locator: str,
    source_kind: str,
    destination: Path,
    allow_network: bool,
) -> dict[str, Any]:
    parsed = urlparse(locator)
    if source_kind == "local_workspace_repo":
        local_root = _resolve_workspace_path(allowed_root=allowed_root, raw_path=locator)
        if not local_root.exists() or not local_root.is_dir():
            raise FileNotFoundError(f"Repository root '{local_root}' is not available.")
        return {
            "workspace_root": _repository_relative_path(allowed_root, local_root),
            "checkout_performed": False,
            "materialization_mode": "direct_workspace_root",
        }

    if destination.exists():
        raise FileExistsError(f"Repository checkout destination '{destination}' already exists.")
    destination.parent.mkdir(parents=True, exist_ok=True)

    if source_kind == "archive_url":
        if parsed.scheme == "file":
            archive_path = _resolve_workspace_path(allowed_root=allowed_root, raw_path=unquote(parsed.path))
        elif parsed.scheme in {"http", "https"}:
            if not allow_network:
                raise PermissionError("Remote archive checkout requires explicit allow_network.")
            with tempfile.NamedTemporaryFile(suffix=Path(parsed.path).suffix or ".zip", delete=False) as handle:
                request = Request(locator, headers={"User-Agent": "agentsFactory-runtime"})
                with urlopen(request, timeout=10) as response:  # noqa: S310
                    handle.write(response.read())
                archive_path = Path(handle.name)
        else:
            raise ValueError(f"Unsupported repository archive locator: {locator}")
        try:
            destination.mkdir(parents=True, exist_ok=False)
            shutil.unpack_archive(str(archive_path), str(destination))
        finally:
            if parsed.scheme in {"http", "https"} and archive_path.exists():
                archive_path.unlink(missing_ok=True)
        return {
            "workspace_root": _repository_relative_path(allowed_root, destination),
            "checkout_performed": True,
            "materialization_mode": "archive_extract",
        }

    if parsed.scheme == "file":
        local_source = _resolve_workspace_path(allowed_root=allowed_root, raw_path=unquote(parsed.path))
        if local_source.is_dir():
            shutil.copytree(local_source, destination)
            return {
                "workspace_root": _repository_relative_path(allowed_root, destination),
                "checkout_performed": True,
                "materialization_mode": "file_url_copy",
            }
        raise FileNotFoundError(f"Repository source '{local_source}' is not available.")

    if parsed.scheme in {"http", "https", "git", "ssh"} or locator.startswith("git@"):
        if not allow_network:
            raise PermissionError("Remote repository checkout requires explicit allow_network.")
        subprocess.run(
            ["git", "clone", "--depth", "1", locator, str(destination)],
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
        )
        return {
            "workspace_root": _repository_relative_path(allowed_root, destination),
            "checkout_performed": True,
            "materialization_mode": "git_clone",
        }

    local_source = _resolve_workspace_path(allowed_root=allowed_root, raw_path=locator)
    if local_source.is_dir():
        shutil.copytree(local_source, destination)
        return {
            "workspace_root": _repository_relative_path(allowed_root, destination),
            "checkout_performed": True,
            "materialization_mode": "local_copy",
        }
    raise ValueError(f"Unsupported repository source locator: {locator}")


def _resolve_repo_root(
    *,
    args: dict[str, Any],
    run_input: dict[str, Any],
    previous_outputs_by_step: dict[str, dict[str, Any]] | None = None,
) -> tuple[Path, Path]:
    allowed_root = _resolve_allowed_root(args)
    source_output = {}
    if previous_outputs_by_step is not None:
        source_output = _resolve_repository_source_output(
            args=args,
            run_input=run_input,
            previous_outputs_by_step=previous_outputs_by_step,
        )
    raw_repo_root = str(
        source_output.get("workspace_root")
        or source_output.get("repo_root")
        or args.get("repo_root")
        or run_input.get("repo_root")
        or "."
    )
    repo_root = _resolve_workspace_path(allowed_root=allowed_root, raw_path=raw_repo_root)
    if not repo_root.exists() or not repo_root.is_dir():
        raise FileNotFoundError(f"Repository root '{repo_root}' is not available.")
    return allowed_root, repo_root


def _repository_relative_path(base: Path, target: Path) -> str:
    return str(target.relative_to(base)).replace("\\", "/")


def _detect_repository_language(path: Path) -> str:
    if path.name == "Dockerfile":
        return "docker"
    if path.name == "Makefile":
        return "make"
    return REPOSITORY_LANGUAGE_BY_SUFFIX.get(path.suffix.lower(), "text")


def _repository_framework_hints(text: str, *, path: Path) -> list[str]:
    lowered = text.lower()
    hints: list[str] = []
    for needle, label in (
        ("fastapi", "fastapi"),
        ("flask", "flask"),
        ("django", "django"),
        ("sqlalchemy", "sqlalchemy"),
        ("celery", "celery"),
        ("react", "react"),
        ("express", "express"),
        ("next", "nextjs"),
        ("pytest", "pytest"),
    ):
        if needle in lowered and label not in hints:
            hints.append(label)
    if path.name == "package.json" and "typescript" in lowered and "typescript" not in hints:
        hints.append("typescript")
    return hints


def _repository_role_hints(relative_path: str, text: str) -> list[str]:
    lowered_path = relative_path.lower()
    lowered_text = text.lower()
    hints: list[str] = []
    for needle, label in (
        ("route", "routes"),
        ("router", "routes"),
        ("service", "services"),
        ("model", "models"),
        ("schema", "schemas"),
        ("config", "config"),
        ("settings", "config"),
        ("task", "tasks"),
        ("job", "jobs"),
        ("test", "tests"),
        ("cli", "cli"),
    ):
        if needle in lowered_path or needle in lowered_text:
            if label not in hints:
                hints.append(label)
    return hints


def _is_repository_entrypoint(path: Path, text: str) -> bool:
    lowered = text.lower()
    if path.name in REPOSITORY_ENTRYPOINT_FILES:
        return True
    return any(
        marker in lowered
        for marker in ("if __name__ == \"__main__\"", "fastapi(", "flask(", "express()", "app.listen(")
    )


def _should_include_repository_path(
    *,
    relative_path: str,
    focus_paths: list[str],
    ignore_patterns: list[str],
) -> bool:
    if focus_paths and not any(relative_path == focus or relative_path.startswith(f"{focus}/") for focus in focus_paths):
        return False
    return not any(fnmatch.fnmatch(relative_path, pattern) for pattern in ignore_patterns)


def _scan_repository_files(
    *,
    repo_root: Path,
    focus_paths: list[str],
    ignore_dirs: set[str],
    ignore_patterns: list[str],
    max_files: int,
    max_total_bytes: int,
) -> tuple[list[Path], dict[str, Any]]:
    selected_files: list[Path] = []
    total_seen = 0
    total_bytes = 0
    partial = False
    truncation_reason = None
    scanned_directories: dict[str, int] = {}

    for current_root, dirs, files in __import__("os").walk(repo_root):
        current_path = Path(current_root)
        relative_dir = "." if current_path == repo_root else _repository_relative_path(repo_root, current_path)
        dirs[:] = sorted(
            [
                item
                for item in dirs
                if item not in ignore_dirs
                and _should_include_repository_path(
                    relative_path=_repository_relative_path(repo_root, current_path / item),
                    focus_paths=focus_paths,
                    ignore_patterns=ignore_patterns,
                )
            ]
        )
        scanned_directories.setdefault(relative_dir, 0)
        for filename in sorted(files):
            file_path = current_path / filename
            relative_path = _repository_relative_path(repo_root, file_path)
            if not _should_include_repository_path(
                relative_path=relative_path,
                focus_paths=focus_paths,
                ignore_patterns=ignore_patterns,
            ):
                continue
            if not file_path.is_file():
                continue
            total_seen += 1
            try:
                size_bytes = file_path.stat().st_size
            except OSError:
                size_bytes = 0
            if len(selected_files) >= max_files:
                partial = True
                truncation_reason = f"max_files={max_files}"
                break
            if total_bytes + size_bytes > max_total_bytes:
                partial = True
                truncation_reason = f"max_total_bytes={max_total_bytes}"
                break
            selected_files.append(file_path)
            total_bytes += size_bytes
            scanned_directories[relative_dir] = scanned_directories.get(relative_dir, 0) + 1
        if partial:
            break

    return selected_files, {
        "total_files_seen": total_seen,
        "included_file_count": len(selected_files),
        "total_bytes_included": total_bytes,
        "partial": partial,
        "truncation_reason": truncation_reason,
        "directory_counts": scanned_directories,
    }


def _repository_imports_and_symbols(*, language: str, text: str) -> tuple[list[str], list[str]]:
    imports: list[str] = []
    symbols: list[str] = []
    if language == "python":
        imports.extend(match.strip() for match in re.findall(r"^\s*from\s+([A-Za-z0-9_\.]+)\s+import\s+", text, flags=re.MULTILINE))
        imports.extend(match.strip() for match in re.findall(r"^\s*import\s+([A-Za-z0-9_\.]+)", text, flags=re.MULTILINE))
        symbols.extend(match.strip() for match in re.findall(r"^\s*def\s+([A-Za-z0-9_]+)\s*\(", text, flags=re.MULTILINE))
        symbols.extend(match.strip() for match in re.findall(r"^\s*class\s+([A-Za-z0-9_]+)\s*[\(:]", text, flags=re.MULTILINE))
    elif language in {"typescript", "javascript"}:
        imports.extend(match.strip() for match in re.findall(r"from\s+['\"]([^'\"]+)['\"]", text))
        imports.extend(match.strip() for match in re.findall(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)", text))
        symbols.extend(match.strip() for match in re.findall(r"export\s+(?:async\s+)?function\s+([A-Za-z0-9_]+)", text))
        symbols.extend(match.strip() for match in re.findall(r"export\s+class\s+([A-Za-z0-9_]+)", text))
        symbols.extend(match.strip() for match in re.findall(r"export\s+const\s+([A-Za-z0-9_]+)", text))
    return sorted(dict.fromkeys(imports)), sorted(dict.fromkeys(symbols))


def _repository_file_role(relative_path: str, role_hints: list[str], *, is_config: bool, is_entrypoint: bool) -> str:
    if is_entrypoint:
        return "entrypoint"
    if is_config:
        return "config"
    return role_hints[0] if role_hints else ("documentation" if relative_path.lower().endswith(".md") else "source")


def _repository_files_from_output(output: dict[str, Any] | None) -> list[str]:
    if not isinstance(output, dict):
        return []
    files = output.get("files")
    if isinstance(files, list):
        return [str(item.get("path")) for item in files if isinstance(item, dict) and str(item.get("path") or "").strip()]
    return []


def _repository_candidate_entrypoints_from_inventory(inventory: dict[str, Any] | None) -> list[str]:
    if not isinstance(inventory, dict):
        return []
    entries = inventory.get("candidate_entrypoints")
    if isinstance(entries, list):
        return [str(item) for item in entries if str(item).strip()]
    return []


def _repository_step_output(previous_outputs_by_step: dict[str, dict[str, Any]], step_name: str) -> dict[str, Any]:
    return previous_outputs_by_step.get(step_name, {}).get("structured_output", {}) if step_name else {}


def _search_web_documents(query: str, *, limit: int, documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    query_terms = [term.lower() for term in query.split() if term.strip()]
    ranked: list[tuple[int, dict[str, Any]]] = []
    for item in documents:
        haystack = " ".join(
            str(value)
            for value in [
                item.get("title"),
                item.get("source"),
                item.get("url"),
                item.get("abstract"),
                " ".join(item.get("highlights", []) if isinstance(item.get("highlights"), list) else []),
                " ".join(item.get("tags", []) if isinstance(item.get("tags"), list) else []),
                item.get("content"),
            ]
            if value is not None
        ).lower()
        score = sum(haystack.count(term) for term in query_terms) if query_terms else 1
        if score <= 0:
            continue
        snippet = (
            item.get("abstract")
            or (item.get("highlights") or [""])[0]
            or item.get("content")
            or item.get("title")
            or ""
        )
        ranked.append(
            (
                score,
                {
                    "title": item.get("title") or "Untitled result",
                    "source": item.get("source") or "bounded_corpus",
                    "url": item.get("url"),
                    "snippet": str(snippet),
                    "score": score,
                },
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in ranked[:limit]]


def _normalize_allowed_domains(value: Any) -> list[str]:
    def _normalize_item(raw: Any) -> str:
        text = str(raw).strip().lower()
        if not text:
            return ""
        parsed = urlparse(text)
        if parsed.scheme and parsed.hostname:
            return parsed.hostname.lower()
        return text.removeprefix(".")

    if isinstance(value, str):
        items = [_normalize_item(item) for item in value.split(",")]
        return [item for item in items if item]
    if isinstance(value, list):
        normalized = []
        for item in value:
            text = _normalize_item(item)
            if text:
                normalized.append(text)
        return normalized
    return []


def _host_allowed(*, host: str, allowed_domains: list[str]) -> bool:
    if not allowed_domains:
        return True
    normalized_host = host.strip().lower()
    return any(
        normalized_host == domain or normalized_host.endswith(f".{domain}")
        for domain in allowed_domains
    )


def _load_text_resource(
    *,
    target: str,
    allowed_root: Path,
    allow_network: bool,
    allowed_domains: list[str],
    timeout_seconds: int,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    parsed = urlparse(target)
    if parsed.scheme in {"http", "https"}:
        if not allow_network:
            raise PermissionError("Remote network access requires allow_network=true.")
        hostname = (parsed.hostname or "").lower()
        if allowed_domains and not _host_allowed(host=hostname, allowed_domains=allowed_domains):
            raise PermissionError(f"Domain '{hostname}' is outside the allowed domain policy.")
        request = Request(target, data=body, headers=headers or {}, method=method.upper())
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            raw = response.read()
            content_type = response.headers.get_content_type() if hasattr(response.headers, "get_content_type") else "text/plain"
            resolved_url = response.geturl()
            return (
                raw.decode("utf-8", errors="replace"),
                resolved_url,
                {
                    "content_type": content_type,
                    "status_code": getattr(response, "status", None) or getattr(response, "code", None),
                    "network_used": True,
                },
            )
    if parsed.scheme == "file":
        local_path = _resolve_workspace_path(allowed_root=allowed_root, raw_path=parsed.path)
    else:
        local_path = _resolve_workspace_path(allowed_root=allowed_root, raw_path=target)
    return (
        local_path.read_text(encoding="utf-8"),
        local_path.as_uri(),
        {
            "content_type": "text/html" if local_path.suffix.lower() in {".html", ".htm"} else "text/plain",
            "status_code": 200,
            "network_used": False,
        },
    )


def _browser_parser_for_html(html: str) -> _BoundedBrowserDocumentParser:
    parser = _BoundedBrowserDocumentParser()
    parser.feed(html)
    return parser


def _browser_title_and_text(parser: _BoundedBrowserDocumentParser) -> tuple[str, str]:
    title = " ".join(parser.title_parts).strip() or "Untitled page"
    visible_text = " ".join(parser.text_parts).strip()
    return title, _truncate_text(visible_text, 4000)


def _browser_selector(args: dict[str, Any], run_input: dict[str, Any]) -> dict[str, Any]:
    selector = args.get("selector") or run_input.get("selector") or {}
    payload = dict(selector) if isinstance(selector, dict) else {}
    for key in ("target_text", "target_href_contains", "target_id", "target_name", "target_index", "form_id", "form_name"):
        value = args.get(key)
        if value is None:
            value = run_input.get(key)
        if value is not None:
            payload[key] = value
    return payload


def _matches_selector(item: dict[str, Any], selector: dict[str, Any]) -> bool:
    if not selector:
        return True
    if selector.get("target_id") and item.get("id") != selector.get("target_id"):
        return False
    if selector.get("target_name") and item.get("name") != selector.get("target_name"):
        return False
    if selector.get("target_text") and selector.get("target_text").lower() not in str(item.get("text") or item.get("label") or "").lower():
        return False
    if selector.get("target_href_contains") and selector.get("target_href_contains").lower() not in str(item.get("href") or "").lower():
        return False
    return True


def _select_browser_link(parser: _BoundedBrowserDocumentParser, selector: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [item for item in parser.links if _matches_selector(item, selector)]
    if not candidates:
        return None
    index = int(selector.get("target_index") or 0)
    index = max(0, min(index, len(candidates) - 1))
    return candidates[index]


def _select_browser_form(parser: _BoundedBrowserDocumentParser, selector: dict[str, Any]) -> dict[str, Any] | None:
    candidates = parser.forms
    if selector.get("form_id"):
        candidates = [item for item in candidates if item.get("id") == selector.get("form_id")]
    if selector.get("form_name"):
        candidates = [item for item in candidates if item.get("name") == selector.get("form_name")]
    return candidates[0] if candidates else None


def _find_form_field(form: dict[str, Any], selector: dict[str, Any], *, collection_key: str) -> dict[str, Any] | None:
    candidates = list(form.get(collection_key, []))
    if selector.get("target_id"):
        candidates = [item for item in candidates if item.get("id") == selector.get("target_id")]
    if selector.get("target_name"):
        candidates = [item for item in candidates if item.get("name") == selector.get("target_name")]
    return candidates[0] if candidates else None


def _browser_form_defaults(form: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in form.get("inputs", []):
        name = str(field.get("name") or "")
        if name:
            values[name] = field.get("value") or ""
    for field in form.get("selects", []):
        name = str(field.get("name") or "")
        if not name:
            continue
        selected_option = next((item for item in field.get("options", []) if item.get("selected")), None)
        if selected_option is None and field.get("options"):
            selected_option = field["options"][0]
        values[name] = selected_option.get("value") if isinstance(selected_option, dict) else ""
    return values


def _build_browser_screenshot_svg(*, title: str, url: str, visible_text: str) -> str:
    snippet = visible_text[:280] or "(no visible text)"
    return (
        "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"960\" height=\"540\">"
        "<rect width=\"100%\" height=\"100%\" fill=\"#f8fafc\"/>"
        f"<text x=\"32\" y=\"52\" font-size=\"24\" fill=\"#0f172a\">{html_escape(title)}</text>"
        f"<text x=\"32\" y=\"88\" font-size=\"12\" fill=\"#475569\">{html_escape(url)}</text>"
        f"<text x=\"32\" y=\"136\" font-size=\"16\" fill=\"#1e293b\">{html_escape(snippet)}</text>"
        "</svg>"
    )


def _resolve_browser_target(
    *,
    args: dict[str, Any],
    run_input: dict[str, Any],
    previous_outputs_by_step: dict[str, dict[str, Any]],
) -> str:
    direct_target = str(args.get("url") or run_input.get("url") or args.get("path") or run_input.get("path") or "").strip()
    if direct_target:
        return direct_target
    source_step = str(args.get("source_step") or "")
    if not source_step:
        return ""
    structured = previous_outputs_by_step.get(source_step, {}).get("structured_output", {})
    if not isinstance(structured, dict):
        return ""
    source_field = str(args.get("source_field") or "")
    if source_field:
        value = structured.get(source_field)
        return str(value).strip() if value is not None else ""
    for candidate_field in ("navigated_url", "submitted_url", "download_url", "url"):
        value = structured.get(candidate_field)
        if value:
            return str(value).strip()
    return ""


def _write_browser_output(
    *,
    allowed_root: Path,
    output_path_value: str,
    content: str,
) -> tuple[str, int]:
    target_path = _resolve_workspace_path(allowed_root=allowed_root, raw_path=output_path_value)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(content, encoding="utf-8")
    relative_path = str(target_path.relative_to(allowed_root) if target_path != allowed_root else ".")
    return relative_path, len(content.encode("utf-8"))


def execute_web_search_workload(
    *,
    step_name: str,
    run_input: dict[str, Any],
    previous_outputs_by_step: dict[str, dict[str, Any]],
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    query_field = str(args.get("query_field") or "query")
    query = str(args.get("query") or run_input.get(query_field) or "").strip()
    if not query:
        raise ValueError(f"Expected non-empty search query from '{query_field}'")
    limit = max(1, min(int(args.get("limit") or 5), 10))
    allow_network = bool(args.get("allow_network") or run_input.get("allow_network"))
    allowed_domains = _normalize_allowed_domains(args.get("allowed_domains") or run_input.get("allowed_domains"))
    supplied_docs = run_input.get("search_documents")
    documents: list[dict[str, Any]]
    if isinstance(supplied_docs, list):
        documents = [item for item in supplied_docs if isinstance(item, dict)]
        source_mode = "supplied_documents"
    else:
        documents = deepcopy(NEWS_ARTICLES) + deepcopy(RESEARCH_ITEMS)
        source_mode = "bounded_corpus"
    source_urls = args.get("source_urls") or run_input.get("source_urls")
    if isinstance(source_urls, list):
        allowed_root = _resolve_allowed_root(args)
        timeout_seconds = max(1, min(int(args.get("timeout_seconds") or 5), 15))
        fetched_sources: list[str] = []
        for raw_target in source_urls[:5]:
            text, resolved_target, metadata = _load_text_resource(
                target=str(raw_target),
                allowed_root=allowed_root,
                allow_network=allow_network,
                allowed_domains=allowed_domains,
                timeout_seconds=timeout_seconds,
            )
            parser = _browser_parser_for_html(text)
            title, visible_text = _browser_title_and_text(parser)
            documents.append(
                {
                    "title": title,
                    "source": "network_source" if metadata["network_used"] else "workspace_source",
                    "url": resolved_target,
                    "content": visible_text or text,
                }
            )
            fetched_sources.append(resolved_target)
        if fetched_sources:
            source_mode = "network_documents"
    results = _search_web_documents(query, limit=limit, documents=documents)
    return {
        "step_name": step_name,
        "summary": f"Found {len(results)} bounded web search results for '{query}'.",
        "structured_output": {
            "query": query,
            "results": results,
            "source_mode": source_mode,
            "network_used": source_mode == "network_documents",
        },
        "content": "\n".join(f"- {item['title']} ({item['source']})" for item in results) or "No results.",
        "adapter": "bounded_web_search",
        "capability_family": "web_search",
    }


def execute_fetch_url_workload(
    *,
    step_name: str,
    run_input: dict[str, Any],
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    target = str(args.get("url") or run_input.get("url") or args.get("path") or run_input.get("path") or "").strip()
    if not target:
        raise ValueError("fetch_url requires url or path.")
    action = str(args.get("action") or "fetch_text").strip().lower()
    if action not in {"fetch_text", "fetch_page"}:
        raise ValueError(f"Unsupported bounded fetch action: {action}")
    timeout_seconds = max(1, min(int(args.get("timeout_seconds") or 5), 15))
    allowed_root = _resolve_allowed_root(args)
    allow_network = bool(args.get("allow_network") or run_input.get("allow_network"))
    allowed_domains = _normalize_allowed_domains(args.get("allowed_domains") or run_input.get("allowed_domains"))
    text, resolved_target, metadata = _load_text_resource(
        target=target,
        allowed_root=allowed_root,
        allow_network=allow_network,
        allowed_domains=allowed_domains,
        timeout_seconds=timeout_seconds,
    )
    parser = _browser_parser_for_html(text)
    title, visible_text = _browser_title_and_text(parser)
    excerpt = visible_text or _truncate_text(text, 4000)
    return {
        "step_name": step_name,
        "summary": f"Fetched bounded content from '{resolved_target}'.",
        "structured_output": {
            "action": action,
            "url": resolved_target,
            "content_type": metadata["content_type"],
            "status_code": metadata["status_code"],
            "network_used": metadata["network_used"],
            "title": title,
            "visible_text": excerpt,
            "link_count": len(parser.links),
            "allowed_domains": allowed_domains,
        },
        "content": excerpt,
        "adapter": "bounded_network_fetch",
        "capability_family": "network_fetch",
    }


def execute_filesystem_workload(
    *,
    step_name: str,
    run_input: dict[str, Any],
    previous_outputs_by_step: dict[str, dict[str, Any]],
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    action = str(args.get("action") or "list").strip().lower()
    allowed_root = _resolve_allowed_root(args)
    target_path = _resolve_workspace_path(
        allowed_root=allowed_root,
        raw_path=str(args.get("path") or run_input.get("path") or "."),
    )

    if action == "list":
        if not target_path.exists():
            raise FileNotFoundError(f"Path '{target_path}' does not exist.")
        entries: list[dict[str, Any]] = []
        for child in sorted(target_path.iterdir(), key=lambda item: item.name)[: int(args.get("limit") or 50)]:
            entries.append(
                {
                    "name": child.name,
                    "path": str(child.relative_to(allowed_root)),
                    "is_dir": child.is_dir(),
                    "size_bytes": child.stat().st_size if child.exists() and child.is_file() else None,
                }
            )
        return {
            "step_name": step_name,
            "summary": f"Listed {len(entries)} filesystem entries under '{target_path.relative_to(allowed_root) if target_path != allowed_root else '.'}'.",
            "structured_output": {"entries": entries, "path": str(target_path)},
            "content": "\n".join(f"- {item['path']}" for item in entries) or "No entries.",
            "adapter": "bounded_filesystem",
            "capability_family": "filesystem_access",
        }

    if action == "read":
        if not target_path.exists() or not target_path.is_file():
            raise FileNotFoundError(f"File '{target_path}' is not available for reading.")
        max_bytes = max(256, min(int(args.get("max_bytes") or 12000), 50000))
        content = target_path.read_text(encoding="utf-8")
        excerpt = content[:max_bytes]
        return {
            "step_name": step_name,
            "summary": f"Read {len(excerpt)} characters from '{target_path.relative_to(allowed_root)}'.",
            "structured_output": {
                "path": str(target_path.relative_to(allowed_root)),
                "content": excerpt,
                "size_bytes": target_path.stat().st_size,
            },
            "content": _truncate_text(excerpt),
            "adapter": "bounded_filesystem",
            "capability_family": "filesystem_access",
        }

    if action == "write":
        allow_write = bool(args.get("allow_write") or run_input.get("allow_write"))
        if not allow_write:
            raise PermissionError("filesystem_write requires explicit allow_write=true.")
        content_value = args.get("content")
        if content_value is None:
            content_value = _resolve_structured_value(
                args=args,
                run_input=run_input,
                previous_outputs_by_step=previous_outputs_by_step,
                default_field="content",
            )
        if content_value is None:
            raise ValueError("filesystem_write requires content, or a valid source_step/source_field.")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        serialized = content_value if isinstance(content_value, str) else json.dumps(content_value, ensure_ascii=False, indent=2)
        target_path.write_text(serialized, encoding="utf-8")
        return {
            "step_name": step_name,
            "summary": f"Wrote {len(serialized)} characters to '{target_path.relative_to(allowed_root)}'.",
            "structured_output": {
                "path": str(target_path.relative_to(allowed_root)),
                "bytes_written": len(serialized.encode('utf-8')),
                "content_preview": _truncate_text(serialized, 500),
            },
            "content": _truncate_text(serialized, 500),
            "adapter": "bounded_filesystem",
            "capability_family": "filesystem_access",
        }

    raise ValueError(f"Unsupported filesystem action: {action}")


def execute_terminal_workload(
    *,
    step_name: str,
    run_input: dict[str, Any],
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    command_value = args.get("command_argv") or run_input.get("command_argv") or args.get("command") or run_input.get("command")
    if isinstance(command_value, str):
        command_argv = shlex.split(command_value)
    elif isinstance(command_value, list):
        command_argv = [str(item) for item in command_value if str(item)]
    else:
        command_argv = []
    if not command_argv:
        raise ValueError("terminal_command requires command_argv or command.")
    executable = command_argv[0]
    if executable not in TERMINAL_COMMAND_ALLOWLIST:
        raise PermissionError(f"Command '{executable}' is not in the bounded terminal allowlist.")
    allowed_root = _resolve_allowed_root(args)
    workdir = _resolve_workspace_path(
        allowed_root=allowed_root,
        raw_path=str(args.get("working_dir") or run_input.get("working_dir") or "."),
    )
    timeout_seconds = max(1, min(int(args.get("timeout_seconds") or 5), 15))
    completed = subprocess.run(
        command_argv,
        cwd=str(workdir),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        shell=False,
        check=False,
    )
    stdout = _truncate_text(completed.stdout or "", 8000)
    stderr = _truncate_text(completed.stderr or "", 4000)
    return {
        "step_name": step_name,
        "summary": f"Executed bounded terminal command '{' '.join(command_argv)}' with return code {completed.returncode}.",
        "structured_output": {
            "command_argv": command_argv,
            "working_dir": str(workdir.relative_to(allowed_root) if workdir != allowed_root else "."),
            "returncode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "success": completed.returncode == 0,
        },
        "content": stdout or stderr or f"Command returned {completed.returncode}.",
        "adapter": "bounded_terminal",
        "capability_family": "terminal_command",
    }


def execute_browser_workload(
    *,
    step_name: str,
    run_input: dict[str, Any],
    previous_outputs_by_step: dict[str, dict[str, Any]],
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    action = str(args.get("action") or run_input.get("action") or "extract_text").strip().lower()
    if action not in {"open_page", "extract_text", "inspect_links", "click", "fill", "select", "submit", "screenshot", "wait_for_target", "save_text", "download_file"}:
        raise ValueError(f"Unsupported bounded browser action: {action}")
    allowed_root = _resolve_allowed_root(args)
    target = _resolve_browser_target(args=args, run_input=run_input, previous_outputs_by_step=previous_outputs_by_step)
    if not target:
        raise ValueError("browser_interaction requires url or path.")
    timeout_seconds = max(1, min(int(args.get("timeout_seconds") or 5), 15))
    allow_network = bool(args.get("allow_network") or run_input.get("allow_network"))
    allowed_domains = _normalize_allowed_domains(args.get("allowed_domains") or run_input.get("allowed_domains"))
    html, resolved_target, metadata = _load_text_resource(
        target=target,
        allowed_root=allowed_root,
        allow_network=allow_network,
        allowed_domains=allowed_domains,
        timeout_seconds=timeout_seconds,
    )
    parser = _browser_parser_for_html(html)
    title, visible_text = _browser_title_and_text(parser)
    links = parser.links[: int(args.get("link_limit") or 20)]
    selector = _browser_selector(args, run_input)
    structured_output = {
        "action": action,
        "url": resolved_target,
        "title": title,
        "visible_text": visible_text,
        "links": links,
        "link_count": len(parser.links),
        "network_used": metadata["network_used"],
        "allowed_domains": allowed_domains,
    }
    if action in {"open_page", "extract_text"}:
        content = structured_output["visible_text"] or title
    elif action == "inspect_links":
        content = "\n".join(f"- {item['text']} -> {item['href']}" for item in links) or "No links."
    elif action == "click":
        link = _select_browser_link(parser, selector)
        if link is None:
            raise ValueError("No bounded browser click target matched the provided selector.")
        destination = urljoin(resolved_target, str(link.get("href") or ""))
        clicked_html, clicked_target, clicked_metadata = _load_text_resource(
            target=destination,
            allowed_root=allowed_root,
            allow_network=allow_network,
            allowed_domains=allowed_domains,
            timeout_seconds=timeout_seconds,
        )
        clicked_parser = _browser_parser_for_html(clicked_html)
        clicked_title, clicked_visible_text = _browser_title_and_text(clicked_parser)
        structured_output.update(
            {
                "clicked_target": {"text": link.get("text"), "href": link.get("href")},
                "navigated_url": clicked_target,
                "title": clicked_title,
                "visible_text": clicked_visible_text,
                "network_used": metadata["network_used"] or clicked_metadata["network_used"],
            }
        )
        content = clicked_visible_text or clicked_title
    elif action == "fill":
        form = _select_browser_form(parser, selector)
        if form is None:
            raise ValueError("No bounded browser form matched the provided selector.")
        field = _find_form_field(form, selector, collection_key="inputs")
        if field is None:
            raise ValueError("No bounded browser input field matched the provided selector.")
        value = args.get("value")
        if value is None:
            value = run_input.get("value")
        if value is None:
            raise ValueError("browser_interaction fill requires a value.")
        form_state = _browser_form_defaults(form)
        if field.get("name"):
            form_state[str(field["name"])] = value
        structured_output.update(
            {
                "filled_field": {"name": field.get("name"), "id": field.get("id"), "type": field.get("type")},
                "value": str(value),
                "form_state": form_state,
                "form_action": urljoin(resolved_target, str(form.get("action") or "")) or resolved_target,
                "form_method": str(form.get("method") or "get").lower(),
            }
        )
        content = f"Filled field '{field.get('name') or field.get('id') or 'input'}' with bounded form state."
    elif action == "select":
        form = _select_browser_form(parser, selector)
        if form is None:
            raise ValueError("No bounded browser form matched the provided selector.")
        field = _find_form_field(form, selector, collection_key="selects")
        if field is None:
            raise ValueError("No bounded browser select field matched the provided selector.")
        value = args.get("value")
        if value is None:
            value = run_input.get("value")
        if value is None:
            raise ValueError("browser_interaction select requires a value.")
        option_values = {str(item.get("value") or "") for item in field.get("options", [])}
        if option_values and str(value) not in option_values:
            raise ValueError(f"Select value '{value}' is not available in the bounded option set.")
        form_state = _browser_form_defaults(form)
        if field.get("name"):
            form_state[str(field["name"])] = value
        structured_output.update(
            {
                "selected_field": {"name": field.get("name"), "id": field.get("id")},
                "value": str(value),
                "options": list(option_values),
                "form_state": form_state,
            }
        )
        content = f"Selected '{value}' in bounded select field '{field.get('name') or field.get('id') or 'select'}'."
    elif action == "submit":
        form = _select_browser_form(parser, selector)
        if form is None:
            raise ValueError("No bounded browser form matched the provided selector.")
        form_values = _browser_form_defaults(form)
        source_step = str(args.get("source_step") or "")
        if source_step:
            previous_structured = previous_outputs_by_step.get(source_step, {}).get("structured_output", {})
            previous_form_state = previous_structured.get("form_state") if isinstance(previous_structured, dict) else None
            if isinstance(previous_form_state, dict):
                for key, value in previous_form_state.items():
                    form_values[str(key)] = value
        override_values = args.get("form_values") or run_input.get("form_values") or {}
        if isinstance(override_values, dict):
            for key, value in override_values.items():
                form_values[str(key)] = value
        method = str(form.get("method") or "get").lower()
        action_target = urljoin(resolved_target, str(form.get("action") or "")) or resolved_target
        if method == "get":
            query = urlencode({key: value for key, value in form_values.items() if value is not None}, doseq=True)
            destination = f"{action_target}?{query}" if query else action_target
            submitted_html, submitted_target, submitted_metadata = _load_text_resource(
                target=action_target,
                allowed_root=allowed_root,
                allow_network=allow_network,
                allowed_domains=allowed_domains,
                timeout_seconds=timeout_seconds,
            )
            submitted_parser = _browser_parser_for_html(submitted_html)
            submitted_title, submitted_visible_text = _browser_title_and_text(submitted_parser)
            structured_output.update(
                {
                    "form_method": method,
                    "submitted_fields": form_values,
                    "submitted_url": destination,
                    "url": submitted_target,
                    "title": submitted_title,
                    "visible_text": submitted_visible_text,
                    "network_used": metadata["network_used"] or submitted_metadata["network_used"],
                }
            )
            content = submitted_visible_text or submitted_title
        elif urlparse(action_target).scheme in {"http", "https"}:
            body = urlencode({key: value for key, value in form_values.items() if value is not None}, doseq=True).encode("utf-8")
            submitted_html, submitted_target, submitted_metadata = _load_text_resource(
                target=action_target,
                allowed_root=allowed_root,
                allow_network=allow_network,
                allowed_domains=allowed_domains,
                timeout_seconds=timeout_seconds,
                method="POST",
                body=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            submitted_parser = _browser_parser_for_html(submitted_html)
            submitted_title, submitted_visible_text = _browser_title_and_text(submitted_parser)
            structured_output.update(
                {
                    "form_method": method,
                    "submitted_fields": form_values,
                    "url": submitted_target,
                    "title": submitted_title,
                    "visible_text": submitted_visible_text,
                    "network_used": metadata["network_used"] or submitted_metadata["network_used"],
                }
            )
            content = submitted_visible_text or submitted_title
        else:
            raise ValueError("Bounded local browser submit only supports GET forms.")
    elif action == "screenshot":
        screenshot_svg = _build_browser_screenshot_svg(title=title, url=resolved_target, visible_text=visible_text)
        structured_output.update(
            {
                "screenshot_format": "svg_preview",
                "screenshot_svg": screenshot_svg,
            }
        )
        content = screenshot_svg
    elif action == "wait_for_target":
        link = _select_browser_link(parser, selector)
        if link is None:
            raise TimeoutError("Target did not become visible within the bounded browser wait window.")
        structured_output.update({"matched_target": link})
        content = f"Target '{link.get('text') or link.get('href')}' is visible."
    elif action == "save_text":
        allow_write = bool(args.get("allow_write") or run_input.get("allow_write"))
        if not allow_write:
            raise PermissionError("browser_interaction save_text requires explicit allow_write=true.")
        output_path_value = str(args.get("output_path") or run_input.get("output_path") or "").strip()
        if not output_path_value:
            raise ValueError("browser_interaction save_text requires output_path.")
        content_field = str(args.get("content_field") or "visible_text").strip().lower()
        if content_field == "title":
            rendered_content = title
        elif content_field == "url":
            rendered_content = resolved_target
        else:
            rendered_content = visible_text or title
        saved_path, bytes_written = _write_browser_output(
            allowed_root=allowed_root,
            output_path_value=output_path_value,
            content=rendered_content,
        )
        structured_output.update(
            {
                "saved_path": saved_path,
                "bytes_written": bytes_written,
                "saved_content_field": content_field,
            }
        )
        content = f"Saved browser page {content_field} to '{saved_path}'."
    elif action == "download_file":
        allow_write = bool(args.get("allow_write") or run_input.get("allow_write"))
        if not allow_write:
            raise PermissionError("browser_interaction download_file requires explicit allow_write=true.")
        output_path_value = str(args.get("output_path") or run_input.get("output_path") or "").strip()
        if not output_path_value:
            raise ValueError("browser_interaction download_file requires output_path.")
        download_target = resolved_target
        selected_link = _select_browser_link(parser, selector) if selector else None
        if selected_link is not None:
            download_target = urljoin(resolved_target, str(selected_link.get("href") or ""))
        downloaded_text, downloaded_target, downloaded_metadata = _load_text_resource(
            target=download_target,
            allowed_root=allowed_root,
            allow_network=allow_network,
            allowed_domains=allowed_domains,
            timeout_seconds=timeout_seconds,
        )
        saved_path, bytes_written = _write_browser_output(
            allowed_root=allowed_root,
            output_path_value=output_path_value,
            content=downloaded_text,
        )
        structured_output.update(
            {
                "download_url": downloaded_target,
                "saved_path": saved_path,
                "bytes_written": bytes_written,
                "content_type": downloaded_metadata["content_type"],
                "status_code": downloaded_metadata["status_code"],
                "network_used": metadata["network_used"] or downloaded_metadata["network_used"],
            }
        )
        if selected_link is not None:
            structured_output["download_target"] = {"text": selected_link.get("text"), "href": selected_link.get("href")}
        content = f"Downloaded bounded text content from '{downloaded_target}' to '{saved_path}'."
    else:
        content = structured_output["visible_text"] or title
    return {
        "step_name": step_name,
        "summary": f"Browser action '{action}' completed for '{resolved_target}'.",
        "structured_output": structured_output,
        "content": content,
        "adapter": "bounded_browser",
        "capability_family": "browser_interaction",
    }


def execute_template_runtime_workload(
    *,
    handler_ref: str,
    step_name: str,
    run_input: dict[str, Any],
    previous_outputs_by_step: dict[str, dict[str, Any]],
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    if handler_ref == "record_normalizer":
        source_step = str(args.get("source_step") or "")
        source_field = str(args.get("source_field") or "records")
        records = (
            previous_outputs_by_step.get(source_step, {}).get("structured_output", {}).get(source_field, [])
            if source_step
            else run_input.get(source_field, [])
        )
        if not isinstance(records, list):
            raise ValueError(f"Expected list input for record_normalizer from '{source_field}'")
        alias_map = args.get("field_aliases") or {}
        canonical_fields = list(args.get("canonical_fields") or [])
        normalized_records: list[dict[str, Any]] = []
        for item in records:
            if not isinstance(item, dict):
                continue
            normalized: dict[str, Any] = {}
            field_pool = {str(key): value for key, value in item.items()}
            keys_to_emit = canonical_fields or list(field_pool.keys())
            for field in keys_to_emit:
                aliases = [field] + [str(alias) for alias in alias_map.get(field, [])]
                value = None
                for alias in aliases:
                    if alias in field_pool:
                        value = field_pool[alias]
                        break
                normalized[field] = value
            normalized_records.append(normalized)
        return {
            "step_name": step_name,
            "summary": f"Normalized {len(normalized_records)} record(s) using bounded template runtime.",
            "structured_output": {
                "normalized_records": normalized_records,
                "template_runtime_kind": handler_ref,
                "field_aliases": alias_map,
            },
            "content": json.dumps(normalized_records, ensure_ascii=False, indent=2),
            "adapter": "materialized_template_runtime",
            "capability_family": "template_runtime",
        }

    if handler_ref == "schema_guard_template":
        source_step = str(args.get("source_step") or "")
        source_field = str(args.get("source_field") or "payload")
        payload = (
            previous_outputs_by_step.get(source_step, {}).get("structured_output", {}).get(source_field, {})
            if source_step
            else run_input.get(source_field, run_input)
        )
        if not isinstance(payload, dict):
            raise ValueError(f"Expected object input for schema_guard_template from '{source_field}'")
        required_fields = [str(item) for item in args.get("required_fields", [])]
        missing_fields = [field for field in required_fields if field not in payload]
        return {
            "step_name": step_name,
            "summary": "Schema guard template passed." if not missing_fields else "Schema guard template found missing fields.",
            "structured_output": {
                "valid": len(missing_fields) == 0,
                "missing_fields": missing_fields,
                "template_runtime_kind": handler_ref,
            },
            "content": json.dumps({"valid": len(missing_fields) == 0, "missing_fields": missing_fields}, ensure_ascii=False, indent=2),
            "adapter": "materialized_template_runtime",
            "capability_family": "template_runtime",
        }

    if handler_ref == "artifact_writer_template":
        content_value = args.get("content")
        if content_value is None:
            content_value = _resolve_structured_value(
                args=args,
                run_input=run_input,
                previous_outputs_by_step=previous_outputs_by_step,
                default_field="content",
            )
        rendered = json.dumps(content_value, ensure_ascii=False, indent=2) if isinstance(content_value, (dict, list)) else str(content_value or "")
        artifact_name = str(args.get("artifact_name") or f"{step_name}.txt")
        return {
            "step_name": step_name,
            "summary": f"Prepared bounded template artifact payload for '{artifact_name}'.",
            "structured_output": {
                "artifact_name": artifact_name,
                "rendered_content": rendered,
                "template_runtime_kind": handler_ref,
            },
            "content": rendered,
            "adapter": "materialized_template_runtime",
            "capability_family": "template_runtime",
        }

    raise ValueError(f"Unsupported bounded template runtime handler: {handler_ref}")


def execute_python_workload(
    *,
    function_name: str,
    step_name: str,
    run_input: dict[str, Any],
    previous_outputs_by_step: dict[str, dict[str, Any]],
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    args = args or {}
    if function_name == "repository_source_resolve":
        allowed_root = _resolve_allowed_root(args)
        source_output = _resolve_repository_source_output(
            args=args,
            run_input=run_input,
            previous_outputs_by_step=previous_outputs_by_step,
        )
        locator = _repository_locator_from_args(args=args, run_input=run_input, source_output=source_output)
        source_kind = _repository_source_kind(locator)
        blocked_reason = None
        workspace_root = None
        requires_checkout = source_kind != "local_workspace_repo"
        if not locator:
            source_kind = "ambiguous_source"
            blocked_reason = "repository_source_locator_missing"
        elif source_kind == "local_workspace_repo":
            repo_root = _resolve_workspace_path(allowed_root=allowed_root, raw_path=locator)
            if not repo_root.exists() or not repo_root.is_dir():
                raise FileNotFoundError(f"Repository root '{repo_root}' is not available.")
            workspace_root = _repository_relative_path(allowed_root, repo_root)
            requires_checkout = False
        elif source_kind == "unsupported_source":
            blocked_reason = "unsupported_repository_source"
        structured_output = {
            "source_locator": locator or None,
            "normalized_locator": locator or None,
            "source_kind": source_kind,
            "workspace_root": workspace_root,
            "requires_checkout": requires_checkout,
            "allow_network_required": source_kind in {"github_repo_url", "git_remote", "archive_url"} and not str(locator).lower().startswith("file://"),
            "supported": blocked_reason is None,
            "blocked_reason": blocked_reason,
        }
        summary = (
            f"Resolved repository source as '{source_kind}'."
            if blocked_reason is None
            else f"Repository source resolution requires review: {blocked_reason}."
        )
        return {
            "step_name": step_name,
            "summary": summary,
            "structured_output": structured_output,
            "content": json.dumps(structured_output, ensure_ascii=False, indent=2),
        }

    if function_name == "repository_checkout":
        allowed_root = _resolve_allowed_root(args)
        source_output = _resolve_repository_source_output(
            args=args,
            run_input=run_input,
            previous_outputs_by_step=previous_outputs_by_step,
        )
        locator = _repository_locator_from_args(args=args, run_input=run_input, source_output=source_output)
        source_kind = str(source_output.get("source_kind") or _repository_source_kind(locator))
        if source_kind in {"unsupported_source", "ambiguous_source"}:
            blocked_reason = str(source_output.get("blocked_reason") or "unsupported_repository_source")
            raise ValueError(f"Repository checkout is blocked: {blocked_reason}.")
        destination = _repository_checkout_destination(
            allowed_root=allowed_root,
            args=args,
            run_input=run_input,
            locator=locator,
            source_kind=source_kind,
        )
        allow_network = bool(args.get("allow_network") or run_input.get("allow_network"))
        checkout = _materialize_repository_source(
            allowed_root=allowed_root,
            locator=locator,
            source_kind=source_kind,
            destination=destination,
            allow_network=allow_network,
        )
        structured_output = {
            "source_locator": locator,
            "source_kind": source_kind,
            "workspace_root": checkout["workspace_root"],
            "repo_root": checkout["workspace_root"],
            "checkout_performed": checkout["checkout_performed"],
            "materialization_mode": checkout["materialization_mode"],
            "destination_root": _repository_relative_path(allowed_root, destination) if checkout["checkout_performed"] else checkout["workspace_root"],
        }
        return {
            "step_name": step_name,
            "summary": (
                "Repository source was materialized into the workspace."
                if checkout["checkout_performed"]
                else "Repository source already pointed at a workspace root."
            ),
            "structured_output": structured_output,
            "content": json.dumps(structured_output, ensure_ascii=False, indent=2),
        }

    if function_name == "repository_inventory":
        allowed_root, repo_root = _resolve_repo_root(
            args=args,
            run_input=run_input,
            previous_outputs_by_step=previous_outputs_by_step,
        )
        ignore_dirs = set(DEFAULT_REPOSITORY_IGNORE_DIRS)
        ignore_dirs.update(_normalize_string_list(args.get("ignore_dirs") or run_input.get("ignore_dirs")))
        ignore_patterns = _normalize_string_list(args.get("ignore_patterns") or run_input.get("ignore_patterns"))
        focus_paths = _normalize_string_list(args.get("focus_paths") or run_input.get("focus_paths"))
        max_files = max(1, min(int(args.get("max_files") or run_input.get("max_files") or 200), 2000))
        max_total_bytes = max(1024, min(int(args.get("max_total_bytes") or run_input.get("max_total_bytes") or 250000), 2_000_000))
        selected_files, scan_summary = _scan_repository_files(
            repo_root=repo_root,
            focus_paths=focus_paths,
            ignore_dirs=ignore_dirs,
            ignore_patterns=ignore_patterns,
            max_files=max_files,
            max_total_bytes=max_total_bytes,
        )
        file_rows: list[dict[str, Any]] = []
        ecosystems: list[str] = []
        key_config_files: list[str] = []
        candidate_entrypoints: list[str] = []
        largest_files: list[dict[str, Any]] = []
        for file_path in selected_files:
            relative_path = _repository_relative_path(repo_root, file_path)
            text = file_path.read_text(encoding="utf-8", errors="replace")
            language = _detect_repository_language(file_path)
            framework_hints = _repository_framework_hints(text, path=file_path)
            role_hints = _repository_role_hints(relative_path, text)
            is_config = file_path.name in REPOSITORY_CONFIG_FILES
            is_entrypoint = _is_repository_entrypoint(file_path, text)
            if is_config and relative_path not in key_config_files:
                key_config_files.append(relative_path)
            if is_entrypoint and relative_path not in candidate_entrypoints:
                candidate_entrypoints.append(relative_path)
            for ecosystem in framework_hints:
                if ecosystem not in ecosystems:
                    ecosystems.append(ecosystem)
            if file_path.name == "package.json" and "node" not in ecosystems:
                ecosystems.append("node")
            if file_path.name == "pyproject.toml" and "python" not in ecosystems:
                ecosystems.append("python")
            try:
                size_bytes = file_path.stat().st_size
            except OSError:
                size_bytes = 0
            file_rows.append(
                {
                    "path": relative_path,
                    "language": language,
                    "size_bytes": size_bytes,
                    "role": _repository_file_role(relative_path, role_hints, is_config=is_config, is_entrypoint=is_entrypoint),
                    "role_hints": role_hints,
                    "framework_hints": framework_hints,
                    "is_entrypoint": is_entrypoint,
                    "is_config": is_config,
                }
            )
            largest_files.append({"path": relative_path, "size_bytes": size_bytes})
        largest_files.sort(key=lambda item: int(item.get("size_bytes") or 0), reverse=True)
        directory_summary = [
            {"path": key, "file_count": value}
            for key, value in sorted((scan_summary.get("directory_counts") or {}).items())
            if value
        ][:20]
        summary = (
            f"Scanned {scan_summary['included_file_count']} repository files under '{_repository_relative_path(allowed_root, repo_root)}'"
            + (f" with partial coverage ({scan_summary['truncation_reason']})." if scan_summary["partial"] else ".")
        )
        structured_output = {
            "repo_root": _repository_relative_path(allowed_root, repo_root),
            "files": file_rows,
            "directory_summary": directory_summary,
            "ecosystems": ecosystems,
            "candidate_entrypoints": candidate_entrypoints,
            "key_config_files": key_config_files,
            "largest_files": largest_files[:10],
            "partial": scan_summary["partial"],
            "truncation_reason": scan_summary["truncation_reason"],
            "coverage": {
                "total_files_seen": scan_summary["total_files_seen"],
                "included_file_count": scan_summary["included_file_count"],
                "total_bytes_included": scan_summary["total_bytes_included"],
                "max_files": max_files,
                "max_total_bytes": max_total_bytes,
            },
            "ignored_directories": sorted(ignore_dirs),
            "focus_paths": focus_paths,
        }
        return {
            "step_name": step_name,
            "summary": summary,
            "structured_output": structured_output,
            "content": json.dumps(
                {
                    "repo_root": structured_output["repo_root"],
                    "ecosystems": ecosystems,
                    "candidate_entrypoints": candidate_entrypoints,
                    "key_config_files": key_config_files,
                    "partial": structured_output["partial"],
                },
                ensure_ascii=False,
                indent=2,
            ),
        }

    if function_name == "source_structure_extract":
        allowed_root, repo_root = _resolve_repo_root(
            args=args,
            run_input=run_input,
            previous_outputs_by_step=previous_outputs_by_step,
        )
        source_step = str(args.get("source_step") or "inventory_repo")
        inventory = _repository_step_output(previous_outputs_by_step, source_step)
        file_paths = _repository_files_from_output(inventory)
        if not file_paths:
            file_paths, _ = _scan_repository_files(
                repo_root=repo_root,
                focus_paths=_normalize_string_list(args.get("focus_paths") or run_input.get("focus_paths")),
                ignore_dirs=DEFAULT_REPOSITORY_IGNORE_DIRS,
                ignore_patterns=[],
                max_files=max(1, min(int(args.get("max_files") or 50), 200)),
                max_total_bytes=max(1024, min(int(args.get("max_total_bytes") or 100000), 500000)),
            )
            file_paths = [_repository_relative_path(repo_root, path) for path in file_paths]
        max_extract_files = max(1, min(int(args.get("max_extract_files") or 50), 200))
        partial = len(file_paths) > max_extract_files
        selected_paths = file_paths[:max_extract_files]
        modules: list[dict[str, Any]] = []
        framework_hints: list[str] = []
        total_imports = 0
        for relative_path in selected_paths:
            file_path = _resolve_workspace_path(allowed_root=allowed_root, raw_path=str(repo_root / relative_path))
            text = file_path.read_text(encoding="utf-8", errors="replace")
            language = _detect_repository_language(file_path)
            imports, symbols = _repository_imports_and_symbols(language=language, text=text)
            frameworks = _repository_framework_hints(text, path=file_path)
            role_hints = _repository_role_hints(relative_path, text)
            for hint in frameworks:
                if hint not in framework_hints:
                    framework_hints.append(hint)
            total_imports += len(imports)
            modules.append(
                {
                    "path": relative_path,
                    "language": language,
                    "imports": imports,
                    "symbols": symbols[:40],
                    "framework_hints": frameworks,
                    "role_hints": role_hints,
                    "line_count": len(text.splitlines()),
                }
            )
        structured_output = {
            "repo_root": _repository_relative_path(allowed_root, repo_root),
            "modules": modules,
            "framework_hints": framework_hints,
            "module_count": len(modules),
            "total_imports": total_imports,
            "partial": partial or bool(inventory.get("partial")),
            "truncation_reason": f"max_extract_files={max_extract_files}" if partial else inventory.get("truncation_reason"),
            "source_inventory_step": source_step,
        }
        return {
            "step_name": step_name,
            "summary": f"Extracted bounded structure from {len(modules)} repository files.",
            "structured_output": structured_output,
            "content": json.dumps(
                {"module_count": len(modules), "framework_hints": framework_hints, "partial": structured_output["partial"]},
                ensure_ascii=False,
                indent=2,
            ),
        }

    if function_name == "repository_relationship_map":
        source_step = str(args.get("source_step") or "extract_structure")
        structure = _repository_step_output(previous_outputs_by_step, source_step)
        modules = [item for item in structure.get("modules", []) if isinstance(item, dict)]
        path_aliases: dict[str, str] = {}
        for module in modules:
            path = str(module.get("path") or "")
            if not path:
                continue
            stem = Path(path).with_suffix("").as_posix()
            dotted = stem.replace("/", ".")
            path_aliases[stem] = path
            path_aliases[dotted] = path
            path_aliases[Path(path).stem] = path
        edges: list[dict[str, Any]] = []
        import_counts: dict[str, int] = {}
        clusters: dict[str, list[str]] = {}
        for module in modules:
            path = str(module.get("path") or "")
            top_level = path.split("/", 1)[0] if "/" in path else "."
            clusters.setdefault(top_level, []).append(path)
            imports = [str(item) for item in module.get("imports", []) if str(item).strip()]
            import_counts[path] = len(imports)
            for imported in imports:
                target_path = path_aliases.get(imported)
                if target_path:
                    edges.append({"source": path, "target": target_path, "import": imported})
        hotspots = [
            {"path": path, "import_count": count}
            for path, count in sorted(import_counts.items(), key=lambda item: item[1], reverse=True)[:10]
        ]
        structured_output = {
            "source_structure_step": source_step,
            "dependency_edges": edges,
            "module_clusters": [{"label": label, "paths": sorted(paths)} for label, paths in sorted(clusters.items())],
            "hotspots": hotspots,
            "boundary_hints": [item["label"] for item in [{"label": label} for label, paths in clusters.items() if len(paths) > 1]],
            "partial": bool(structure.get("partial")),
        }
        return {
            "step_name": step_name,
            "summary": f"Mapped {len(edges)} bounded dependency edge(s) across {len(modules)} module(s).",
            "structured_output": structured_output,
            "content": json.dumps({"dependency_edges": len(edges), "clusters": len(clusters), "partial": structured_output["partial"]}, ensure_ascii=False, indent=2),
        }

    if function_name == "repository_execution_surface_inspect":
        allowed_root, repo_root = _resolve_repo_root(
            args=args,
            run_input=run_input,
            previous_outputs_by_step=previous_outputs_by_step,
        )
        inventory_step = str(args.get("inventory_step") or "inventory_repo")
        structure_step = str(args.get("structure_step") or "extract_structure")
        inventory = _repository_step_output(previous_outputs_by_step, inventory_step)
        structure = _repository_step_output(previous_outputs_by_step, structure_step)
        file_rows = [item for item in inventory.get("files", []) if isinstance(item, dict)]
        build_entry_points: list[dict[str, Any]] = []
        runtime_entry_points: list[dict[str, Any]] = []
        task_surfaces: list[dict[str, Any]] = []
        config_surfaces: list[str] = []
        integration_hints: list[str] = []
        for file_row in file_rows:
            relative_path = str(file_row.get("path") or "")
            file_path = _resolve_workspace_path(allowed_root=allowed_root, raw_path=str(repo_root / relative_path))
            text = file_path.read_text(encoding="utf-8", errors="replace")
            if file_path.name == "package.json":
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = {}
                scripts = payload.get("scripts", {}) if isinstance(payload, dict) else {}
                for name, command in scripts.items() if isinstance(scripts, dict) else []:
                    build_entry_points.append({"surface": "package_script", "name": str(name), "command": str(command)})
            if file_path.name == "pyproject.toml":
                if "[project.scripts]" in text:
                    build_entry_points.append({"surface": "pyproject", "name": "project.scripts", "command": "declared"})
                if "[tool.poetry.scripts]" in text:
                    build_entry_points.append({"surface": "pyproject", "name": "tool.poetry.scripts", "command": "declared"})
            if bool(file_row.get("is_entrypoint")):
                runtime_entry_points.append({"path": relative_path, "reason": "candidate_entrypoint"})
            if any(token in relative_path.lower() for token in ("task", "job", "script", "cli")):
                task_surfaces.append({"path": relative_path, "role": "task_surface"})
            if bool(file_row.get("is_config")):
                config_surfaces.append(relative_path)
            for hint in _repository_framework_hints(text, path=file_path):
                if hint not in integration_hints:
                    integration_hints.append(hint)
        structure_frameworks = [str(item) for item in structure.get("framework_hints", []) if str(item).strip()]
        for hint in structure_frameworks:
            if hint not in integration_hints:
                integration_hints.append(hint)
        structured_output = {
            "inventory_step": inventory_step,
            "structure_step": structure_step,
            "build_entry_points": build_entry_points,
            "runtime_entry_points": runtime_entry_points or [{"path": item, "reason": "inventory_candidate"} for item in _repository_candidate_entrypoints_from_inventory(inventory)],
            "task_surfaces": task_surfaces,
            "config_surfaces": sorted(dict.fromkeys(config_surfaces)),
            "integration_hints": integration_hints,
            "partial": bool(inventory.get("partial")) or bool(structure.get("partial")),
        }
        return {
            "step_name": step_name,
            "summary": f"Inspected repository execution surfaces and found {len(structured_output['runtime_entry_points'])} runtime entry point hint(s).",
            "structured_output": structured_output,
            "content": json.dumps(
                {
                    "build_entry_points": len(build_entry_points),
                    "runtime_entry_points": len(structured_output["runtime_entry_points"]),
                    "task_surfaces": len(task_surfaces),
                    "partial": structured_output["partial"],
                },
                ensure_ascii=False,
                indent=2,
            ),
        }

    if function_name == "repository_evidence_bundle":
        inventory_step = str(args.get("inventory_step") or "inventory_repo")
        structure_step = str(args.get("structure_step") or "extract_structure")
        relationship_step = str(args.get("relationship_step") or "map_relationships")
        execution_surface_step = str(args.get("execution_surface_step") or "inspect_execution_surfaces")
        inventory = _repository_step_output(previous_outputs_by_step, inventory_step)
        structure = _repository_step_output(previous_outputs_by_step, structure_step)
        relationship = _repository_step_output(previous_outputs_by_step, relationship_step)
        execution_surface = _repository_step_output(previous_outputs_by_step, execution_surface_step)
        partial_steps = [
            step_name
            for step_name, payload in (
                (inventory_step, inventory),
                (structure_step, structure),
                (relationship_step, relationship),
                (execution_surface_step, execution_surface),
            )
            if isinstance(payload, dict) and payload.get("partial")
        ]
        unknowns: list[str] = []
        if partial_steps:
            unknowns.append(f"Partial coverage affected: {', '.join(partial_steps)}")
        if not execution_surface.get("runtime_entry_points"):
            unknowns.append("No strong runtime entry point was detected from the bounded scan.")
        if not relationship.get("dependency_edges"):
            unknowns.append("No local dependency edges were derived from the bounded structure extraction.")
        evidence_bundle = {
            "repo_root": inventory.get("repo_root"),
            "coverage": {
                "partial": bool(partial_steps),
                "partial_steps": partial_steps,
                "inventory": inventory.get("coverage", {}),
            },
            "inventory_summary": {
                "file_count": len(inventory.get("files", [])) if isinstance(inventory.get("files"), list) else 0,
                "ecosystems": inventory.get("ecosystems", []),
                "candidate_entrypoints": inventory.get("candidate_entrypoints", []),
                "key_config_files": inventory.get("key_config_files", []),
            },
            "structure_summary": {
                "module_count": structure.get("module_count", 0),
                "framework_hints": structure.get("framework_hints", []),
            },
            "relationship_summary": {
                "dependency_edge_count": len(relationship.get("dependency_edges", [])) if isinstance(relationship.get("dependency_edges"), list) else 0,
                "module_clusters": relationship.get("module_clusters", []),
                "hotspots": relationship.get("hotspots", []),
            },
            "execution_surface_summary": {
                "build_entry_points": execution_surface.get("build_entry_points", []),
                "runtime_entry_points": execution_surface.get("runtime_entry_points", []),
                "task_surfaces": execution_surface.get("task_surfaces", []),
                "config_surfaces": execution_surface.get("config_surfaces", []),
                "integration_hints": execution_surface.get("integration_hints", []),
            },
            "file_refs": sorted(
                dict.fromkeys(
                    [str(item.get("path")) for item in inventory.get("files", []) if isinstance(item, dict) and item.get("path")]
                    + [str(item.get("path")) for item in structure.get("modules", []) if isinstance(item, dict) and item.get("path")]
                )
            )[:100],
            "unknowns": unknowns,
            "confidence_markers": {
                "coverage_status": "partial" if partial_steps else "bounded_complete",
                "module_count": structure.get("module_count", 0),
                "dependency_edge_count": len(relationship.get("dependency_edges", [])) if isinstance(relationship.get("dependency_edges"), list) else 0,
            },
        }
        return {
            "step_name": step_name,
            "summary": "Packaged bounded repository evidence bundle." + (" Coverage is partial." if partial_steps else ""),
            "structured_output": {
                "evidence_bundle": evidence_bundle,
                "partial": bool(partial_steps),
                "unknowns": unknowns,
            },
            "content": json.dumps(evidence_bundle, ensure_ascii=False, indent=2),
        }

    if function_name == "architecture_report_brief":
        source_step = str(args.get("source_step") or "build_evidence_bundle")
        source_field = str(args.get("source_field") or "evidence_bundle")
        evidence_step = _repository_step_output(previous_outputs_by_step, source_step)
        evidence_bundle = evidence_step.get(source_field) if isinstance(evidence_step, dict) else None
        if not isinstance(evidence_bundle, dict):
            raise ValueError("architecture_report_brief requires a repository evidence bundle.")
        objective = str(args.get("objective") or run_input.get("objective") or "Generate a grounded architecture report.")
        audience = str(args.get("audience") or run_input.get("audience") or "engineering reviewers")
        execution_surface = evidence_bundle.get("execution_surface_summary", {})
        relationship_summary = evidence_bundle.get("relationship_summary", {})
        report_brief = {
            "objective": objective,
            "audience": audience,
            "repo_root": evidence_bundle.get("repo_root"),
            "coverage": evidence_bundle.get("coverage", {}),
            "inventory_summary": evidence_bundle.get("inventory_summary", {}),
            "structure_summary": evidence_bundle.get("structure_summary", {}),
            "relationship_summary": relationship_summary,
            "execution_surface_summary": execution_surface,
            "key_points": [
                f"Module count: {evidence_bundle.get('structure_summary', {}).get('module_count', 0)}",
                f"Dependency edge count: {relationship_summary.get('dependency_edge_count', 0)}",
                f"Runtime entry points: {len(execution_surface.get('runtime_entry_points', [])) if isinstance(execution_surface, dict) else 0}",
            ],
            "citation_candidates": list(evidence_bundle.get("file_refs", []))[:20],
            "unknowns": list(evidence_bundle.get("unknowns", [])),
        }
        return {
            "step_name": step_name,
            "summary": "Prepared a grounded architecture-report brief from the bounded repository evidence bundle.",
            "structured_output": {
                "report_brief": report_brief,
                "objective": objective,
                "audience": audience,
            },
            "content": json.dumps(report_brief, ensure_ascii=False, indent=2),
        }

    if function_name == "architecture_report_synthesis":
        source_step = str(args.get("source_step") or "build_evidence_bundle")
        source_field = str(args.get("source_field") or "evidence_bundle")
        evidence_step = _repository_step_output(previous_outputs_by_step, source_step)
        evidence_bundle = evidence_step.get(source_field) if isinstance(evidence_step, dict) else None
        if not isinstance(evidence_bundle, dict):
            raise ValueError("architecture_report_synthesis requires a repository evidence bundle.")
        objective = str(args.get("objective") or run_input.get("objective") or "Produce an architecture-oriented repository report.")
        audience = str(args.get("audience") or run_input.get("audience") or "engineering operators")
        inventory_summary = evidence_bundle.get("inventory_summary", {})
        structure_summary = evidence_bundle.get("structure_summary", {})
        relationship_summary = evidence_bundle.get("relationship_summary", {})
        execution_surface_summary = evidence_bundle.get("execution_surface_summary", {})
        unknowns = evidence_bundle.get("unknowns", []) if isinstance(evidence_bundle.get("unknowns"), list) else []
        runtime_entry_points = execution_surface_summary.get("runtime_entry_points", []) if isinstance(execution_surface_summary, dict) else []
        build_entry_points = execution_surface_summary.get("build_entry_points", []) if isinstance(execution_surface_summary, dict) else []
        report_markdown = "\n".join(
            [
                "# Architecture Report",
                "",
                f"Objective: {objective}",
                f"Audience: {audience}",
                "",
                "## Repository Overview",
                f"- Repo root: {evidence_bundle.get('repo_root') or '.'}",
                f"- Files analyzed: {inventory_summary.get('file_count', 0)}",
                f"- Ecosystems: {', '.join(inventory_summary.get('ecosystems', [])) or 'none detected'}",
                f"- Coverage status: {evidence_bundle.get('confidence_markers', {}).get('coverage_status', 'unknown')}",
                "",
                "## Module / Structure Summary",
                f"- Module count: {structure_summary.get('module_count', 0)}",
                f"- Framework hints: {', '.join(structure_summary.get('framework_hints', [])) or 'none'}",
                f"- Dependency edges: {relationship_summary.get('dependency_edge_count', 0)}",
                "",
                "## Execution Surfaces",
                f"- Runtime entry points: {len(runtime_entry_points)}",
                f"- Build entry points: {len(build_entry_points)}",
                f"- Task surfaces: {len(execution_surface_summary.get('task_surfaces', [])) if isinstance(execution_surface_summary, dict) else 0}",
                "",
                "## Candidate Entrypoints",
                *[
                    f"- {item}"
                    for item in inventory_summary.get("candidate_entrypoints", [])[:10]
                ],
                "",
                "## Config / Integration Surfaces",
                *[
                    f"- {item}"
                    for item in execution_surface_summary.get("config_surfaces", [])[:10]
                ],
                *[
                    f"- integration hint: {item}"
                    for item in execution_surface_summary.get("integration_hints", [])[:10]
                ],
                "",
                "## Risks / Unknowns",
                *([f"- {item}" for item in unknowns] or ["- No major unknowns were surfaced by this bounded analysis pass."]),
                "",
                "## Suggested Next Inspection Points",
                *[
                    f"- {item}"
                    for item in evidence_bundle.get("file_refs", [])[:10]
                ],
            ]
        ).strip()
        return {
            "step_name": step_name,
            "summary": "Synthesized bounded architecture report from repository evidence.",
            "structured_output": {
                "report_markdown": report_markdown,
                "objective": objective,
                "audience": audience,
                "unknown_count": len(unknowns),
                "evidence_source_step": source_step,
            },
            "content": report_markdown,
        }

    if function_name == "architecture_report_render":
        source_step = str(args.get("source_step") or "synthesize_architecture_report")
        source_output = _repository_step_output(previous_outputs_by_step, source_step)
        report_markdown = source_output.get("report_markdown") if isinstance(source_output, dict) else None
        if not isinstance(report_markdown, str):
            citations = source_output.get("citations", []) if isinstance(source_output, dict) else []
            report_markdown = "\n".join(
                [
                    "# Architecture Report",
                    "",
                    "## Repository Overview",
                    str(source_output.get("overview") or "No overview was generated."),
                    "",
                    "## Subsystem / Module Map",
                    *[f"- {item}" for item in source_output.get("subsystem_map", [])[:12]],
                    "",
                    "## Execution Surfaces",
                    *[f"- {item}" for item in source_output.get("execution_surfaces", [])[:12]],
                    "",
                    "## Dependencies",
                    *[f"- {item}" for item in source_output.get("dependencies", [])[:12]],
                    "",
                    "## Risks / Unknowns",
                    *[f"- {item}" for item in source_output.get("risks_unknowns", [])[:12]],
                    "",
                    "## Suggested Next Inspection Points",
                    *[f"- {item}" for item in source_output.get("next_inspection_points", [])[:12]],
                    "",
                    "## Cited File References",
                    *[f"- {item}" for item in citations[:20]],
                ]
            ).strip()
        coverage_status = str(args.get("coverage_status") or run_input.get("coverage_status") or "")
        if not coverage_status:
            brief_step = str(args.get("brief_step") or "draft_architecture_report_brief")
            brief_output = _repository_step_output(previous_outputs_by_step, brief_step)
            coverage_status = (
                str(
                    ((brief_output.get("report_brief") or {}).get("coverage") or {}).get("coverage_status")
                    or ((brief_output.get("report_brief") or {}).get("coverage") or {}).get("partial")
                    or "bounded_complete"
                )
                if isinstance(brief_output, dict)
                else "bounded_complete"
            )
        if "## Coverage Note" not in report_markdown:
            report_markdown = (
                report_markdown.rstrip()
                + "\n\n## Coverage Note\n"
                + f"- Coverage status: {coverage_status}\n"
            )
        return {
            "step_name": step_name,
            "summary": "Rendered the architecture-report draft into delivery-ready markdown.",
            "structured_output": {
                "report_markdown": report_markdown,
                "coverage_status": coverage_status,
            },
            "content": report_markdown,
        }

    if function_name == "architecture_report_quality_gate":
        report_step = str(args.get("report_step") or "render_architecture_report")
        brief_step = str(args.get("brief_step") or "draft_architecture_report_brief")
        report_output = _repository_step_output(previous_outputs_by_step, report_step)
        brief_output = _repository_step_output(previous_outputs_by_step, brief_step)
        report_markdown = str(report_output.get("report_markdown") or "")
        report_brief = brief_output.get("report_brief") if isinstance(brief_output, dict) else {}
        required_sections = [
            "## Repository Overview",
            "## Execution Surfaces",
            "## Risks / Unknowns",
            "## Coverage Note",
        ]
        missing_sections = [section for section in required_sections if section not in report_markdown]
        citation_candidates = [
            str(item) for item in (report_brief.get("citation_candidates", []) if isinstance(report_brief, dict) else []) if str(item).strip()
        ]
        cited_file_refs = [item for item in citation_candidates if item in report_markdown]
        valid = not missing_sections and bool(cited_file_refs)
        review_required = not valid
        quality_result = {
            "valid": valid,
            "missing_sections": missing_sections,
            "cited_file_refs": cited_file_refs[:20],
            "citation_candidate_count": len(citation_candidates),
            "review_required": review_required,
            "report_markdown": report_markdown,
        }
        return {
            "step_name": step_name,
            "summary": "Architecture report passed the bounded quality gate." if valid else "Architecture report requires review after the bounded quality gate.",
            "structured_output": quality_result,
            "content": json.dumps(quality_result, ensure_ascii=False, indent=2),
        }

    if function_name == "research_briefing_render":
        source_step = str(args.get("source_step") or "synthesize_research_briefing")
        source_output = previous_outputs_by_step.get(source_step, {}).get("structured_output", {})
        summary = str(source_output.get("summary") or "").strip()
        key_findings = [
            str(item).strip()
            for item in (source_output.get("key_findings", []) if isinstance(source_output, dict) else [])
            if str(item).strip()
        ]
        unknowns = [
            str(item).strip()
            for item in (source_output.get("unknowns", []) if isinstance(source_output, dict) else [])
            if str(item).strip()
        ]
        title = str(args.get("title") or run_input.get("title") or "Research Briefing").strip()
        report_markdown = "\n".join(
            [
                f"# {title}",
                "",
                "## Summary",
                summary or "No summary was generated.",
                "",
                "## Key Findings",
                *([f"- {item}" for item in key_findings] or ["- No key findings were extracted."]),
                "",
                "## Unknowns",
                *([f"- {item}" for item in unknowns] or ["- No material unknowns were reported."]),
            ]
        )
        return {
            "step_name": step_name,
            "summary": "Rendered the structured research briefing into markdown.",
            "structured_output": {
                "report_markdown": report_markdown,
                "title": title,
                "finding_count": len(key_findings),
                "unknown_count": len(unknowns),
            },
            "content": report_markdown,
        }

    if function_name == "parse_json_payload":
        source_field = str(args.get("source_field") or "raw_json")
        raw_value = run_input.get(source_field)
        parsed = raw_value
        if isinstance(raw_value, str):
            parsed = json.loads(raw_value)
        if not isinstance(parsed, (dict, list)):
            raise ValueError(f"Expected JSON object or array in run_input['{source_field}']")
        return {
            "step_name": step_name,
            "summary": f"Parsed JSON payload from '{source_field}'.",
            "structured_output": {
                "parsed_payload": parsed,
                "source_field": source_field,
            },
            "content": json.dumps(parsed, ensure_ascii=False, indent=2),
        }

    if function_name == "deduplicate_items":
        source_step = str(args.get("source_step") or "")
        source_field = str(args.get("source_field") or "items")
        unique_key = str(args.get("unique_key") or "id")
        if source_step:
            items = previous_outputs_by_step.get(source_step, {}).get("structured_output", {}).get(source_field, [])
        else:
            items = run_input.get(source_field, [])
        if not isinstance(items, list):
            raise ValueError(f"Expected list input for deduplicate_items from '{source_field}'")
        seen: set[str] = set()
        deduplicated = []
        duplicate_count = 0
        for item in items:
            if isinstance(item, dict):
                key = str(item.get(unique_key) or json.dumps(item, sort_keys=True))
            else:
                key = str(item)
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            deduplicated.append(item)
        return {
            "step_name": step_name,
            "summary": f"Deduplicated {len(items)} items down to {len(deduplicated)} unique items.",
            "structured_output": {
                "items": deduplicated,
                "duplicate_count": duplicate_count,
                "unique_key": unique_key,
            },
            "content": "\n".join(f"- {item}" for item in deduplicated[:10]),
        }

    if function_name == "rank_candidates":
        source_step = str(args.get("source_step") or "")
        source_field = str(args.get("source_field") or "candidates")
        score_field = str(args.get("score_field") or "score")
        if source_step:
            candidates = previous_outputs_by_step.get(source_step, {}).get("structured_output", {}).get(source_field, [])
        else:
            candidates = run_input.get(source_field, [])
        if not isinstance(candidates, list):
            raise ValueError(f"Expected list input for rank_candidates from '{source_field}'")
        ranked = sorted(
            [item for item in candidates if isinstance(item, dict)],
            key=lambda item: float(item.get(score_field) or 0),
            reverse=True,
        )
        return {
            "step_name": step_name,
            "summary": f"Ranked {len(ranked)} candidates using '{score_field}'.",
            "structured_output": {
                "ranked_candidates": ranked,
                "score_field": score_field,
            },
            "content": "\n".join(
                f"- {item.get('name') or item.get('id') or 'candidate'}: {item.get(score_field)}"
                for item in ranked[:10]
            ),
        }

    if function_name == "validate_schema_contract":
        required_fields = args.get("required_fields") or []
        source_field = str(args.get("source_field") or "payload")
        payload = run_input.get(source_field, run_input)
        if not isinstance(payload, dict):
            raise ValueError(f"Expected object input for validate_schema_contract from '{source_field}'")
        missing_fields = [field for field in required_fields if field not in payload]
        return {
            "step_name": step_name,
            "summary": "Validation completed with no missing required fields." if not missing_fields else "Validation found missing required fields.",
            "structured_output": {
                "valid": len(missing_fields) == 0,
                "missing_fields": missing_fields,
                "validated_field": source_field,
            },
            "content": "valid" if not missing_fields else ", ".join(missing_fields),
        }

    if function_name == "fetch_articles":
        limit = int(args.get("limit", 5))
        topic = str(run_input.get("topic") or "AI systems").strip()
        selected = deepcopy(NEWS_ARTICLES[:limit])
        return {
            "step_name": step_name,
            "summary": f"Fetched {len(selected)} articles for topic '{topic}'.",
            "structured_output": {
                "topic": topic,
                "articles": selected,
            },
            "content": "\n".join(f"- {item['title']} ({item['source']})" for item in selected),
        }

    if function_name == "collect_sources":
        limit = int(args.get("limit", 4))
        theme = str(run_input.get("theme") or "AI delivery").strip()
        items = deepcopy(RESEARCH_ITEMS[:limit])
        return {
            "step_name": step_name,
            "summary": f"Collected {len(items)} research items for theme '{theme}'.",
            "structured_output": {
                "theme": theme,
                "items": items,
            },
            "content": "\n".join(f"- {item['title']} [{item['kind']}]" for item in items),
        }

    if function_name == "normalize_items":
        collected = previous_outputs_by_step.get("collect_sources", {}).get("structured_output", {}).get("items", [])
        normalized = [
            {
                **item,
                "normalized_tags": [tag.lower().replace(" ", "-") for tag in item.get("tags", [])],
                "source_label": f"{item.get('source')}::{item.get('kind')}",
            }
            for item in collected
        ]
        return {
            "step_name": step_name,
            "summary": f"Normalized {len(normalized)} research items.",
            "structured_output": {"items": normalized},
            "content": "\n".join(f"- {item['title']} ({', '.join(item['normalized_tags'])})" for item in normalized),
        }

    raise ValueError(f"Unsupported python_function workload: {function_name}")


def build_artifact_outputs(
    *,
    writer_kind: str,
    run_input: dict[str, Any],
    previous_outputs_by_step: dict[str, dict[str, Any]],
    args: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    args = args or {}
    if writer_kind == "write_artifact":
        source_step = str(args.get("source_step") or "")
        if not source_step and previous_outputs_by_step:
            source_step = list(previous_outputs_by_step.keys())[-1]
        source_field = str(args.get("source_field") or "")
        payload = previous_outputs_by_step.get(source_step or "", {}).get("structured_output")
        if source_field and isinstance(payload, dict):
            payload = payload.get(source_field)
        if payload is None:
            payload = run_input
        artifact_format = str(args.get("artifact_format") or "json").strip().lower()
        filename = str(args.get("artifact_filename") or f"artifact_output.{ 'json' if artifact_format == 'json' else 'txt' }")
        if artifact_format == "json":
            rendered_content: Any = {
                "kind": "write_artifact",
                "source_step": source_step,
                "payload": payload,
            }
            content_format = "json"
            artifact_type = "written_artifact_json"
        elif artifact_format in {"text", "markdown"}:
            if isinstance(payload, str):
                rendered_content = payload
            else:
                rendered_content = json.dumps(payload, ensure_ascii=False, indent=2)
            content_format = "markdown" if artifact_format == "markdown" else "text"
            artifact_type = f"written_artifact_{artifact_format}"
        elif artifact_format == "csv":
            rows = payload if isinstance(payload, list) else []
            if not all(isinstance(item, dict) for item in rows):
                raise ValueError("write_artifact csv format requires a list of object rows.")
            csv_fields = [str(item) for item in args.get("csv_fields", []) if str(item).strip()]
            if not csv_fields:
                seen_fields: list[str] = []
                for row in rows:
                    for key in row.keys():
                        text = str(key)
                        if text not in seen_fields:
                            seen_fields.append(text)
                csv_fields = seen_fields
            csv_buffer = StringIO()
            writer = csv.DictWriter(csv_buffer, fieldnames=csv_fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in csv_fields})
            rendered_content = csv_buffer.getvalue()
            content_format = "text"
            artifact_type = "written_artifact_csv"
        else:
            raise ValueError(f"Unsupported write_artifact format: {artifact_format}")
        return [
            {
                "filename": filename,
                "artifact_type": artifact_type,
                "content": rendered_content,
                "content_format": content_format,
            }
        ]

    if writer_kind == "news_digest":
        selected = previous_outputs_by_step.get("select_top_articles", {}).get("structured_output", {}).get("selected_articles", [])
        summaries = previous_outputs_by_step.get("summarize_articles", {}).get("structured_output", {}).get("summaries", [])
        markdown = previous_outputs_by_step.get("generate_digest", {}).get("content") or "# AI News Digest\n\n_No content generated._"
        json_payload = {
            "kind": "news_digest",
            "topic": run_input.get("topic") or "AI systems",
            "selected_articles": selected,
            "summaries": summaries,
            "generated_digest_markdown": markdown,
        }
        return [
            {
                "filename": "digest.md",
                "artifact_type": "digest_markdown",
                "content": markdown,
                "content_format": "markdown",
            },
            {
                "filename": "digest.json",
                "artifact_type": "digest_json",
                "content": json_payload,
                "content_format": "json",
            },
        ]

    if writer_kind == "weekly_research_report":
        filtered = previous_outputs_by_step.get("filter_relevant", {}).get("structured_output", {}).get("relevant_items", [])
        summaries = previous_outputs_by_step.get("summarize_items", {}).get("structured_output", {}).get("summaries", [])
        markdown = previous_outputs_by_step.get("generate_report", {}).get("content") or "# AI Weekly Research Report\n\n_No content generated._"
        json_payload = {
            "kind": "weekly_research_report",
            "theme": run_input.get("theme") or "AI delivery",
            "relevant_items": filtered,
            "summaries": summaries,
            "generated_report_markdown": markdown,
        }
        return [
            {
                "filename": "weekly_report.md",
                "artifact_type": "weekly_report_markdown",
                "content": markdown,
                "content_format": "markdown",
            },
            {
                "filename": "weekly_report.json",
                "artifact_type": "weekly_report_json",
                "content": json_payload,
                "content_format": "json",
            },
        ]

    if writer_kind == "markdown_report":
        # Generic markdown report: uses LLM-produced content if available,
        # otherwise assembles from previous step outputs.
        output_filename = str(args.get("output_filename") or "report.md")

        # Prefer LLM-produced content (injected by _llm_produce_artifact_content)
        llm_content = args.get("_llm_produced_content")
        if isinstance(llm_content, str) and llm_content.strip():
            markdown_body = llm_content
        else:
            # Find the best markdown content from previous outputs
            markdown_body = ""
            for step_name in reversed(list(previous_outputs_by_step.keys())):
                step_out = previous_outputs_by_step[step_name]
                content = step_out.get("content", "")
                if isinstance(content, str) and content.strip() and "# " in content:
                    markdown_body = content
                    break

        # If no markdown found, build from structured_output
        if not markdown_body:
            sections: list[str] = []
            for step_name, step_out in previous_outputs_by_step.items():
                summary = step_out.get("summary", "")
                content = step_out.get("content", "")
                if summary:
                    sections.append(f"## {step_name}\n\n{summary}")
                if isinstance(content, str) and content.strip():
                    sections.append(content)
            if sections:
                markdown_body = "\n\n".join(sections)
            else:
                topic = run_input.get("topic") or run_input.get("query") or "Report"
                markdown_body = f"# {topic}\n\n_Report generated from workflow execution._"

        json_payload = {
            "kind": "markdown_report",
            "output_filename": output_filename,
            "step_count": len(previous_outputs_by_step),
            "source_steps": list(previous_outputs_by_step.keys()),
            "generated_markdown": markdown_body,
        }
        return [
            {
                "filename": output_filename,
                "artifact_type": "report_markdown",
                "content": markdown_body,
                "content_format": "markdown",
            },
            {
                "filename": output_filename.replace(".md", ".json"),
                "artifact_type": "report_json",
                "content": json_payload,
                "content_format": "json",
            },
        ]

    raise ValueError(f"Unsupported artifact_writer workload: {writer_kind}")
