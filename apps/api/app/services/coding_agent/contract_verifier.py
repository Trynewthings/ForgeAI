"""Coding-agent contract verifier — STRICT field-name validation
between iterations.

The output_policy check (in service.py) only asks "did the file exist
and was nothing extra written?". That misses the most damaging class
of failure we observed on the spare-parts scenario: the agent writes a
CSV at the right path with totally invented column names (``sku_name``
instead of ``part_name``), so downstream steps can't read it. The
output_policy passes but the dataflow contract is broken.

This module sits between the runner and the iteration loop: after the
sandbox returns, we read each ``ExpectedOutput`` whose
``required_fields`` is set, parse its header (CSV) or top-level keys
(JSON), and compare against the strict list. Any mismatch becomes a
structured ``ContractFieldViolation`` that the prompt builder feeds
back to the LLM as machine-readable correction.

Hard invariants:
    * The verifier never raises — every failure becomes a structured
      violation entry with an ``error_type`` tag.
    * The verifier never modifies files. Read-only diagnostics.
    * Files without ``required_fields`` are SKIPPED (advisory hint
      only); skipped count is recorded in the validation envelope.
    * Format support today: csv (header line), json (top-level dict
      keys or first-element keys for arrays), jsonl (first-record
      keys). Other formats return ``format_unsupported`` and the loop
      treats those as advisory.

Field comparison rules:
    * Missing: a field listed in required_fields that the produced
      file does NOT publish. ALWAYS a violation.
    * Unexpected: a field the file publishes that's not in
      required_fields. Violation only when ``forbid_extra_fields``.
    * Comparison is case-sensitive and whitespace-stripped — the LLM
      must use exact field names from the schema.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
from typing import Any, Optional

from app.schemas.coding_agent import (
    ContractFieldViolation,
    ContractValidation,
    ExpectedOutput,
)

logger = logging.getLogger(__name__)


# Read at most this many bytes from a produced file when probing its
# header / keys. CSV header lives at the top; JSON keys may be spread
# across a much larger top-level dict (an intermediate dataset bundle
# can be megabytes). 4 MiB is generous but bounded — enough to catch
# top-level keys of any realistic intermediate file.
_MAX_HEAD_BYTES = 4 * 1024 * 1024


def validate_contract(
    *,
    expected_outputs: list[ExpectedOutput],
) -> ContractValidation:
    """Walk each declared expected output and verify its field set."""
    result = ContractValidation()

    for eo in expected_outputs:
        required = list(eo.required_fields or [])
        if not required:
            result.skipped_files += 1
            continue
        result.checked_files += 1

        violation = _validate_one(eo)
        if violation is not None:
            result.violations.append(violation)
    if result.violations:
        logger.info(
            "contract_verifier.violations_found count=%d checked=%d skipped=%d",
            len(result.violations),
            result.checked_files,
            result.skipped_files,
        )
    return result


def _validate_one(eo: ExpectedOutput) -> Optional[ContractFieldViolation]:
    if not os.path.exists(eo.locator):
        return ContractFieldViolation(
            locator=eo.locator,
            error_type="file_not_found",
            expected_schema=list(eo.required_fields or []),
            detail=f"Declared output file does not exist at {eo.locator}.",
        )

    suffix = _suffix(eo.locator)
    try:
        head = _read_head_bytes(eo.locator, _MAX_HEAD_BYTES)
    except OSError as exc:
        return ContractFieldViolation(
            locator=eo.locator,
            error_type="file_unreadable",
            expected_schema=list(eo.required_fields or []),
            detail=f"OS error reading file: {exc}",
        )

    actual_fields, fmt = _extract_fields(suffix=suffix, head=head)
    if fmt == "unsupported":
        return ContractFieldViolation(
            locator=eo.locator,
            error_type="format_unsupported",
            expected_schema=list(eo.required_fields or []),
            detail=(
                f"Could not extract fields from {eo.locator}; "
                "format not supported by the contract verifier."
            ),
        )

    expected = list(eo.required_fields or [])
    expected_set = set(expected)
    actual_set = set(actual_fields)

    missing = [f for f in expected if f not in actual_set]
    unexpected: list[str] = []
    if eo.forbid_extra_fields:
        unexpected = [f for f in actual_fields if f not in expected_set]

    if not missing and not unexpected:
        return None

    if missing and unexpected:
        error_type = "missing_and_unexpected"
    elif missing:
        error_type = "missing_fields"
    else:
        error_type = "unexpected_fields"

    return ContractFieldViolation(
        locator=eo.locator,
        error_type=error_type,
        expected_schema=expected,
        actual_schema=list(actual_fields),
        missing_fields=missing,
        unexpected_fields=unexpected,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _suffix(path: str) -> str:
    return os.path.splitext(path)[1].lower().lstrip(".")


def _read_head_bytes(path: str, n: int) -> bytes:
    with open(path, "rb") as fh:
        return fh.read(n)


def _extract_fields(*, suffix: str, head: bytes) -> tuple[list[str], str]:
    """Return ``(fields, format_tag)``. format_tag is 'csv' / 'json' /
    'jsonl' / 'unsupported'.

    Extension-driven path first; if extension is unknown (typical of
    chained intermediate files like ``coding_agent_step_NN.out``),
    sniff the content to decide whether it parses as JSON or CSV.
    """
    if suffix in {"csv", "tsv"}:
        delim = "\t" if suffix == "tsv" else ","
        return _csv_header(head, delim), "csv"
    if suffix == "jsonl" or suffix == "ndjson":
        return _jsonl_first_keys(head), "jsonl"
    if suffix == "json":
        return _json_top_keys(head), "json"

    # Unknown extension — try to detect the format by content. Order
    # matters: prefer JSON detection (cheap, unambiguous) before CSV
    # (which would happily parse anything with commas as a "header").
    sniffed = _sniff_format_from_head(head)
    if sniffed == "json":
        return _json_top_keys(head), "json"
    if sniffed == "jsonl":
        return _jsonl_first_keys(head), "jsonl"
    if sniffed == "csv":
        return _csv_header(head, ","), "csv"
    return [], "unsupported"


def _sniff_format_from_head(head: bytes) -> str:
    """Lightweight content-based format detection.

    Returns one of "json" / "jsonl" / "csv" / "unknown".

    JSON detection accepts truncated heads — when ``json.loads`` fails,
    we still call it JSON if the head clearly opens with ``{`` or ``[``
    and the structural top-level walk produces at least one key.
    Truncated heads of large JSON objects are common in this pipeline
    (intermediate ``.out`` files in the chain are megabytes).
    """
    text = head.decode("utf-8", errors="replace").lstrip()
    if not text:
        return "unknown"
    first_char = text[0]
    if first_char in "{[":
        # Pass 1: clean parse.
        try:
            json.loads(text)
            return "json"
        except json.JSONDecodeError:
            pass
        # Pass 2: raw_decode (handles trailing garbage).
        try:
            json.JSONDecoder().raw_decode(text)
            return "json"
        except (json.JSONDecodeError, ValueError):
            pass
        # Pass 3: structural — head opened with `{`/`[` and has at
        # least one extractable top-level key.
        if first_char == "{" and _walk_top_level_keys(text):
            return "json"
        if first_char == "[":
            # Array of objects: first non-blank line that's an object.
            stripped = text.lstrip()
            if stripped.startswith("[") and "{" in stripped[:64]:
                return "json"
        # JSONL fallback — first non-blank line is a JSON object.
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("{"):
                try:
                    json.loads(line)
                    return "jsonl"
                except json.JSONDecodeError:
                    return "unknown"
            return "unknown"
        return "unknown"
    # CSV: first line has a delimiter and looks header-shaped.
    first_line = text.splitlines()[0]
    if "," in first_line and "{" not in first_line and "}" not in first_line:
        if first_line.count(",") >= 1:
            return "csv"
    return "unknown"


def _csv_header(head: bytes, delim: str) -> list[str]:
    text = head.decode("utf-8", errors="replace")
    try:
        reader = csv.reader(io.StringIO(text), delimiter=delim)
        for row in reader:
            return [_norm(cell) for cell in row]
    except csv.Error:
        return []
    return []


def _json_top_keys(head: bytes) -> list[str]:
    """Best-effort top-level key extraction from a JSON head.

    1. Try ``json.loads`` on the whole head.
    2. Try ``json.JSONDecoder.raw_decode`` to read just the first
       complete JSON value (handles trailing garbage / truncation).
    3. As a structural fallback (head was cut MID-object), walk the
       string with a brace-depth counter and pluck every key at
       depth 0. Robust against multi-megabyte top-level dicts whose
       head is the only thing we read.
    """
    text = head.decode("utf-8", errors="replace").strip()
    if not text:
        return []

    # Pass 1: full-doc parse.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None

    # Pass 2: raw_decode — accepts trailing garbage / truncation.
    if data is None:
        try:
            decoder = json.JSONDecoder()
            data, _ = decoder.raw_decode(text)
        except (json.JSONDecodeError, ValueError):
            data = None

    if data is not None:
        if isinstance(data, dict):
            return [_norm(k) for k in data.keys()]
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return [_norm(k) for k in data[0].keys()]
        return []

    # Pass 3: structural top-level key walk — no full JSON parse,
    # tracks brace / bracket depth and extracts ``"key":`` pairs at
    # depth 1 (i.e. immediately inside the outer ``{ ... }``).
    return _walk_top_level_keys(text)


def _walk_top_level_keys(text: str) -> list[str]:
    """Scan a (possibly truncated) JSON object string and return its
    top-level keys without doing a full parse.

    Handles strings (with escape sequences), nested braces / brackets,
    and unbalanced trailing characters. Returns ``[]`` when the head
    isn't shaped like an object.
    """
    if not text or text[0] != "{":
        return []

    keys: list[str] = []
    depth = 0
    in_string = False
    escape = False
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            # Start of a string. If we're at depth 1, this string MAY
            # be a top-level key — confirm by scanning past the
            # closing quote and whitespace, then checking for ``:``.
            string_start = i + 1
            j = i + 1
            local_escape = False
            while j < n:
                cj = text[j]
                if local_escape:
                    local_escape = False
                elif cj == "\\":
                    local_escape = True
                elif cj == '"':
                    break
                j += 1
            if j >= n:
                # Truncated string — give up.
                break
            string_end = j
            # Look ahead for ``:``.
            k = j + 1
            while k < n and text[k] in " \t\n\r":
                k += 1
            if depth == 1 and k < n and text[k] == ":":
                keys.append(text[string_start:string_end])
            i = j + 1
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                # Closed the outer object — we're done.
                break
        i += 1
    return [_norm(k) for k in keys]


def _jsonl_first_keys(head: bytes) -> list[str]:
    text = head.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return [_norm(k) for k in obj.keys()]
    return []


def _norm(s: Any) -> str:
    return str(s).strip()


__all__ = [
    "validate_contract",
]
