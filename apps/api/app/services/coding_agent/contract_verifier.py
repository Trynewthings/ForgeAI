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
import pickle
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
    """Walk each declared expected output and verify its field set
    AND (7.6F) row-count threshold.

    A file is checked when EITHER ``required_fields`` is non-empty
    OR ``min_rows`` is set. Both checks run independently — a file
    can pass the schema check and still fail min_rows or vice versa.
    """
    result = ContractValidation()

    for eo in expected_outputs:
        required = list(eo.required_fields or [])
        min_rows = eo.min_rows
        if not required and (min_rows is None or min_rows <= 0):
            result.skipped_files += 1
            continue
        result.checked_files += 1

        # Schema check (7.6 + earlier).
        if required:
            violation = _validate_one(eo)
            if violation is not None:
                result.violations.append(violation)

        # Row-count check (7.6F). Run independently — a header-only
        # CSV can pass schema validation while failing min_rows.
        if min_rows is not None and min_rows > 0:
            row_violation = _validate_min_rows(eo)
            if row_violation is not None:
                result.violations.append(row_violation)
    if result.violations:
        logger.info(
            "contract_verifier.violations_found count=%d checked=%d skipped=%d",
            len(result.violations),
            result.checked_files,
            result.skipped_files,
        )
    return result


def _validate_min_rows(eo: ExpectedOutput) -> Optional[ContractFieldViolation]:
    """7.6F — count data rows in the produced file and compare to
    ``eo.min_rows``. Returns a violation when the count is below the
    threshold; None on pass or when we genuinely cannot count rows
    (in which case schema validation was the only enforceable check).
    """
    min_rows = eo.min_rows or 0
    if min_rows <= 0:
        return None
    if not os.path.exists(eo.locator):
        # file_not_found is already covered by the schema-side check;
        # don't double-emit it.
        if list(eo.required_fields or []):
            return None
        return ContractFieldViolation(
            locator=eo.locator,
            error_type="file_not_found",
            detail=f"Declared output file does not exist at {eo.locator}.",
        )

    suffix = _suffix(eo.locator)
    try:
        actual = _count_data_rows(eo.locator, suffix)
    except OSError as exc:
        return ContractFieldViolation(
            locator=eo.locator,
            error_type="file_unreadable",
            detail=f"OS error counting rows: {exc}",
        )
    if actual is None:
        # Format we can't count — degrade silently. The schema-side
        # check (if any) will already have caught format_unsupported.
        return None
    if actual >= min_rows:
        return None

    return ContractFieldViolation(
        locator=eo.locator,
        error_type="empty_intermediate_artifact",
        actual_row_count=actual,
        expected_min_rows=min_rows,
        detail=(
            f"Produced file has {actual} data row(s); contract requires "
            f"at least {min_rows}. Header-only or empty intermediate "
            "artifacts break the downstream chain — your script must "
            "preserve and transform real upstream rows, not just write "
            "a header."
        ),
    )


def _count_data_rows(path: str, suffix: str) -> Optional[int]:
    """Best-effort data-row count for tabular / line-delimited files.

    * CSV / TSV — count lines minus 1 for header.
    * JSONL / NDJSON — count non-blank lines.
    * JSON top-level array — count items in the array.
    * JSON top-level dict — treated as 1 (single-record file).
    * Pickle of dict-of-DataFrames — sum of inner DataFrame lengths
      (each entry is one upstream dataset). When inner values aren't
      DataFrames, falls back to top-level key count.
    * Pickle of pandas DataFrame — len(df).
    * Pickle of list — len.

    Returns None when format cannot be determined / counted.
    Bounded read so a multi-GB file doesn't blow memory; we stream
    and count newlines for line-based formats.
    """
    if suffix in {"csv", "tsv"}:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            count = 0
            for _ in fh:
                count += 1
            # Subtract 1 for the header line.
            return max(0, count - 1)
    if suffix in {"jsonl", "ndjson"}:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if line.strip())
    if suffix == "json":
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(payload, list):
            return len(payload)
        if isinstance(payload, dict):
            return 1
        return None
    if suffix in {"pkl", "pickle"}:
        return _count_pickle_rows(path)
    # Unknown extension — sniff. Read a small head to decide format,
    # then count over the full file using the resolved kind.
    try:
        head = _read_head_bytes(path, _MAX_HEAD_BYTES)
    except OSError:
        return None
    fmt = _sniff_format_from_head(head)
    if fmt == "csv":
        return _count_data_rows(path, "csv")
    if fmt == "jsonl":
        return _count_data_rows(path, "jsonl")
    if fmt == "json":
        return _count_data_rows(path, "json")
    if fmt == "pickle":
        return _count_pickle_rows(path)
    return None


def _count_pickle_rows(path: str) -> Optional[int]:
    """Defensive row counter for pickle files. Returns None on any
    deserialization error so the verifier degrades to advisory."""
    try:
        with open(path, "rb") as fh:
            payload = pickle.load(fh)
    except Exception:  # noqa: BLE001
        return None
    # pandas DataFrame
    if hasattr(payload, "shape") and hasattr(payload, "columns"):
        try:
            return int(payload.shape[0])
        except Exception:  # noqa: BLE001
            pass
    if isinstance(payload, dict):
        # dict-of-DataFrames: sum inner row counts when possible;
        # otherwise count keys (each key = one dataset) so an
        # empty bundle (zero-key dict) still trips ``min_rows>=1``.
        total = 0
        any_inner_df = False
        for v in payload.values():
            if hasattr(v, "shape") and hasattr(v, "columns"):
                any_inner_df = True
                try:
                    total += int(v.shape[0])
                except Exception:  # noqa: BLE001
                    pass
        if any_inner_df:
            return total
        return len(payload)
    if isinstance(payload, list):
        return len(payload)
    return 1


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

    actual_fields, fmt = _extract_fields(suffix=suffix, head=head, path=eo.locator)
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


def _extract_fields(*, suffix: str, head: bytes, path: Optional[str] = None) -> tuple[list[str], str]:
    """Return ``(fields, format_tag)``. format_tag is 'csv' / 'json' /
    'jsonl' / 'pickle' / 'unsupported'.

    Extension-driven path first; if extension is unknown (typical of
    chained intermediate files like ``coding_agent_step_NN.out``),
    sniff the content to decide whether it parses as JSON or CSV
    or Python pickle.

    Pickle support: the chained intermediate step files emitted by
    pandas-using nodes often serialize ``dict[str, DataFrame]`` via
    ``pickle.dump`` (the natural way to preserve a dataset bundle
    in-memory). We treat the dict's top-level keys as the field set,
    same shape the dataset_collection contract expects. ``path`` is
    needed because pickle requires reading the full file (the head
    we already buffered is fine for pickle since pickles are small
    relative to the 4MB cap; the path is used as a fallback when
    truncation matters).
    """
    if suffix in {"csv", "tsv"}:
        delim = "\t" if suffix == "tsv" else ","
        return _csv_header(head, delim), "csv"
    if suffix == "jsonl" or suffix == "ndjson":
        return _jsonl_first_keys(head), "jsonl"
    if suffix == "json":
        return _json_top_keys(head), "json"
    if suffix in {"pkl", "pickle"}:
        keys = _pickle_top_keys(head, path=path)
        return (keys, "pickle") if keys else ([], "unsupported")

    # Unknown extension — sniff. Order matters: pickle detection
    # is unambiguous (magic byte ``0x80``) so it goes first, then
    # JSON (cheap), then CSV (which would happily call any line
    # with commas a "header").
    sniffed = _sniff_format_from_head(head)
    if sniffed == "pickle":
        keys = _pickle_top_keys(head, path=path)
        return (keys, "pickle") if keys else ([], "unsupported")
    if sniffed == "json":
        return _json_top_keys(head), "json"
    if sniffed == "jsonl":
        return _jsonl_first_keys(head), "jsonl"
    if sniffed == "csv":
        return _csv_header(head, ","), "csv"
    return [], "unsupported"


def _sniff_format_from_head(head: bytes) -> str:
    """Lightweight content-based format detection.

    Returns one of "json" / "jsonl" / "csv" / "pickle" / "unknown".

    Pickle: bytes start with the protocol byte ``\\x80`` followed by
    a small protocol number (Python 2-5). Cheap to check and
    unambiguous against text formats.

    JSON detection accepts truncated heads — when ``json.loads`` fails,
    we still call it JSON if the head clearly opens with ``{`` or ``[``
    and the structural top-level walk produces at least one key.
    Truncated heads of large JSON objects are common in this pipeline
    (intermediate ``.out`` files in the chain are megabytes).
    """
    # Pickle protocol byte first — unambiguous and cheap.
    if len(head) >= 2 and head[0] == 0x80 and head[1] in {0x02, 0x03, 0x04, 0x05}:
        return "pickle"
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


def _pickle_top_keys(head: bytes, *, path: Optional[str] = None) -> list[str]:
    """Load a pickle from ``head`` (preferred) or ``path`` (fallback)
    and return its top-level dict keys, or single-key list when the
    payload is a DataFrame / Series / dict-like.

    The chained intermediate ``.out`` files in the spare-parts flow
    contain ``dict[str, pandas.DataFrame]`` produced by ``pickle.dump``
    — the keys ARE the dataset bundle names that downstream specs
    expect to read. Returning those keys lets the contract verifier
    honor ``required_fields`` for ``dataset_collection`` outputs.

    Defensive — never raises, returns ``[]`` on any deserialization
    error.
    """
    payload: Any = None
    if head:
        try:
            payload = pickle.loads(head)
        except Exception:  # noqa: BLE001
            payload = None
    if payload is None and path:
        try:
            with open(path, "rb") as fh:
                payload = pickle.load(fh)
        except Exception:  # noqa: BLE001
            return []
    if payload is None:
        return []
    if isinstance(payload, dict):
        return [_norm(k) for k in payload.keys()]
    # pandas DataFrame: columns are the field set.
    cols = getattr(payload, "columns", None)
    if cols is not None:
        try:
            return [_norm(c) for c in list(cols)]
        except Exception:  # noqa: BLE001
            pass
    return []


def _norm(s: Any) -> str:
    return str(s).strip()


__all__ = [
    "validate_contract",
]
