"""Data profiling pass — peek at the real input files BEFORE the spec pass
runs an LLM, so the spec agent's ``schema_hint`` fields aren't
hallucinated.

The profiler reads each file under the input locator (or the single
file, if the locator IS a file) and emits a compact per-file summary:

    * ``kind`` — ``csv`` / ``json`` / ``jsonl`` / ``text`` / ``binary``
    * ``size_bytes`` — actual file size on disk
    * ``columns`` — CSV header fields (or top-level JSON keys), empty for
      raw text
    * ``sample_rows`` — up to N structured rows (dict[str,str]) for CSV,
      the first N list items for JSON arrays, the first N lines for text
    * ``row_count_hint`` — cheap line count, capped so huge files stay
      bounded

Hard invariants:
    * Never reads more than ``max_bytes_per_file`` from any file.
    * Never returns binary payloads — binaries are reported as
      ``{"kind": "binary", ...}`` without a sample.
    * Crashes are captured as ``{"kind": "error", "error": "..."}``
      entries rather than raising — the spec pass runs against whatever
      the profiler successfully saw.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_DEFAULT_MAX_BYTES_PER_FILE = 64 * 1024  # 64 KiB header is plenty for schema + samples
_DEFAULT_SAMPLE_ROWS = 5
_DEFAULT_MAX_FILES = 32

_CSV_SUFFIXES = {".csv", ".tsv"}
_JSON_SUFFIXES = {".json"}
_JSONL_SUFFIXES = {".jsonl", ".ndjson"}
_TEXT_SUFFIXES = {".txt", ".md", ".rst", ".log", ".yaml", ".yml", ".toml", ".ini"}


@dataclass
class FileProfile:
    path: str
    kind: str
    size_bytes: int
    columns: list[str] = field(default_factory=list)
    sample_rows: list[Any] = field(default_factory=list)
    row_count_hint: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
        }
        if self.columns:
            out["columns"] = self.columns
        if self.sample_rows:
            out["sample_rows"] = self.sample_rows
        if self.row_count_hint is not None:
            out["row_count_hint"] = self.row_count_hint
        if self.error:
            out["error"] = self.error
        return out


@dataclass
class DataProfilingResult:
    locator: str
    files: list[FileProfile]
    missing: bool = False
    truncated: bool = False
    sampled_at_bytes: int = _DEFAULT_MAX_BYTES_PER_FILE
    sampled_rows: int = _DEFAULT_SAMPLE_ROWS

    def to_dict(self) -> dict[str, Any]:
        return {
            "locator": self.locator,
            "missing": self.missing,
            "truncated": self.truncated,
            "sampled_at_bytes": self.sampled_at_bytes,
            "sampled_rows": self.sampled_rows,
            "files": [f.to_dict() for f in self.files],
        }


def profile_locator(
    locator: str | None,
    *,
    max_bytes_per_file: int = _DEFAULT_MAX_BYTES_PER_FILE,
    sample_rows: int = _DEFAULT_SAMPLE_ROWS,
    max_files: int = _DEFAULT_MAX_FILES,
) -> DataProfilingResult:
    """Profile the files under ``locator``.

    ``locator`` may be a directory (all files profiled), a single file,
    or a non-path string (``None`` / ``""`` / sentinel). Non-paths return
    an empty, ``missing=True`` result so callers can branch without
    raising.
    """
    if not locator:
        return DataProfilingResult(locator="", files=[], missing=True)

    path = Path(locator).expanduser()
    result = DataProfilingResult(
        locator=str(path),
        files=[],
        sampled_at_bytes=max_bytes_per_file,
        sampled_rows=sample_rows,
    )

    if not path.exists():
        result.missing = True
        return result

    file_paths: list[Path]
    if path.is_dir():
        file_paths = sorted(p for p in path.iterdir() if p.is_file())
        if len(file_paths) > max_files:
            file_paths = file_paths[:max_files]
            result.truncated = True
    else:
        file_paths = [path]

    for fp in file_paths:
        result.files.append(
            _profile_single_file(
                fp, max_bytes=max_bytes_per_file, sample_rows=sample_rows
            )
        )
    return result


def _profile_single_file(
    path: Path,
    *,
    max_bytes: int,
    sample_rows: int,
) -> FileProfile:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return FileProfile(
            path=str(path), kind="error", size_bytes=0, error=f"stat_failed:{exc}"
        )

    suffix = path.suffix.lower()
    try:
        with open(path, "rb") as fh:
            head = fh.read(max_bytes + 1)
    except OSError as exc:
        return FileProfile(
            path=str(path), kind="error", size_bytes=size, error=f"read_failed:{exc}"
        )

    truncated = len(head) > max_bytes
    head = head[:max_bytes]

    if suffix in _CSV_SUFFIXES:
        return _profile_csv(path, head, size, sample_rows, delimiter=",")
    if suffix == ".tsv":
        return _profile_csv(path, head, size, sample_rows, delimiter="\t")
    if suffix in _JSONL_SUFFIXES:
        return _profile_jsonl(path, head, size, sample_rows)
    if suffix in _JSON_SUFFIXES:
        return _profile_json(path, head, size, sample_rows)
    if suffix in _TEXT_SUFFIXES or _looks_textual(head):
        return _profile_text(path, head, size, sample_rows, truncated=truncated)

    return FileProfile(
        path=str(path), kind="binary", size_bytes=size
    )


def _profile_csv(
    path: Path, head: bytes, size: int, sample_rows: int, *, delimiter: str
) -> FileProfile:
    try:
        text = head.decode("utf-8", errors="replace")
    except Exception as exc:  # defensive: decode on replace shouldn't raise
        return FileProfile(path=str(path), kind="csv", size_bytes=size, error=str(exc))
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = list(reader)
    except csv.Error as exc:
        return FileProfile(
            path=str(path), kind="csv", size_bytes=size, error=f"csv_parse_error:{exc}"
        )
    if not rows:
        return FileProfile(path=str(path), kind="csv", size_bytes=size)
    header = [cell for cell in rows[0]]
    body = rows[1 : 1 + sample_rows]
    # Drop trailing empty row if the head cut mid-line.
    sample = []
    for row in body:
        if all(cell == "" for cell in row):
            continue
        sample.append(dict(zip(header, row + [""] * max(0, len(header) - len(row)))))
    return FileProfile(
        path=str(path),
        kind="csv",
        size_bytes=size,
        columns=header,
        sample_rows=sample,
        row_count_hint=_cheap_line_count(path),
    )


def _profile_json(
    path: Path, head: bytes, size: int, sample_rows: int
) -> FileProfile:
    text = head.decode("utf-8", errors="replace").strip()
    # If the head was truncated we can't parse reliably; fall back to text.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _profile_text(path, head, size, sample_rows, truncated=True, fallback_from="json")
    if isinstance(data, dict):
        return FileProfile(
            path=str(path),
            kind="json",
            size_bytes=size,
            columns=list(data.keys())[:64],
            sample_rows=[_shorten_json_value(data)],
        )
    if isinstance(data, list):
        sample = data[:sample_rows]
        columns: list[str] = []
        if sample and isinstance(sample[0], dict):
            columns = list(sample[0].keys())[:64]
        return FileProfile(
            path=str(path),
            kind="json",
            size_bytes=size,
            columns=columns,
            sample_rows=[_shorten_json_value(v) for v in sample],
        )
    return FileProfile(
        path=str(path),
        kind="json",
        size_bytes=size,
        sample_rows=[_shorten_json_value(data)],
    )


def _profile_jsonl(
    path: Path, head: bytes, size: int, sample_rows: int
) -> FileProfile:
    text = head.decode("utf-8", errors="replace")
    samples: list[Any] = []
    columns: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        samples.append(_shorten_json_value(obj))
        if isinstance(obj, dict) and not columns:
            columns = list(obj.keys())[:64]
        if len(samples) >= sample_rows:
            break
    return FileProfile(
        path=str(path),
        kind="jsonl",
        size_bytes=size,
        columns=columns,
        sample_rows=samples,
        row_count_hint=_cheap_line_count(path),
    )


def _profile_text(
    path: Path,
    head: bytes,
    size: int,
    sample_rows: int,
    *,
    truncated: bool = False,
    fallback_from: str | None = None,
) -> FileProfile:
    text = head.decode("utf-8", errors="replace")
    lines = text.splitlines()[:sample_rows]
    prof = FileProfile(
        path=str(path),
        kind="text",
        size_bytes=size,
        sample_rows=lines,
        row_count_hint=_cheap_line_count(path),
    )
    if fallback_from:
        prof.error = f"fallback_from_{fallback_from}:head_truncated={truncated}"
    return prof


def _looks_textual(head: bytes) -> bool:
    if not head:
        return True
    sample = head[:1024]
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    # Reject if > 5% null bytes (crude binary heuristic).
    nulls = sample.count(b"\x00")
    return nulls / max(1, len(sample)) < 0.05


def _cheap_line_count(path: Path, *, cap: int = 5000) -> int | None:
    try:
        count = 0
        with open(path, "rb") as fh:
            for _ in fh:
                count += 1
                if count >= cap:
                    return cap  # capped — signal "at least this many"
        return count
    except OSError:
        return None


def _shorten_json_value(value: Any, *, max_str_len: int = 200) -> Any:
    """Compact a JSON value so profiling summaries stay small in the prompt."""
    if isinstance(value, str):
        if len(value) <= max_str_len:
            return value
        return value[:max_str_len] + "…"
    if isinstance(value, dict):
        return {
            k: _shorten_json_value(v, max_str_len=max_str_len)
            for k, v in list(value.items())[:32]
        }
    if isinstance(value, list):
        return [_shorten_json_value(v, max_str_len=max_str_len) for v in value[:8]]
    return value


__all__ = [
    "DataProfilingResult",
    "FileProfile",
    "profile_locator",
]
