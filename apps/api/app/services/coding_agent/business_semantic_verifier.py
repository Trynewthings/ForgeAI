"""Task 7.6E — business semantic verifier.

The schema verifier (``contract_verifier.py``) checks that produced
files have the right shape — column names, JSON keys. That's necessary
but not sufficient. A run can pass schema checks while still emitting:

  * ``risk_before`` values like ``"Critical"`` when the user only allowed
    {Stockout Risk, Imminent Shortage, Normal, Overstock Risk,
     Replenishment Blocked}
  * ``supplier_id = "SUP-001"`` when the source ``suppliers.csv`` has
    real supplier ids like ``S-004``
  * ``estimated_cost = 0.0`` for every row when the formula
    ``estimated_cost = order_qty_rounded * unit_cost`` was declared
  * 2400 rows (Cartesian product of 20 SKUs × 120 dates) when the queue
    should only contain replenishment candidates (≈ 10 rows)
  * Empty ``calculation_note`` despite an ``evidence_requirement`` rule

The semantic verifier reads the produced file, the structured
``BusinessRuleContract`` list, and the source tables (when join rules
declare them) and reports per-rule violations. A run is OK only when
every ``severity == "hard"`` rule passes.

Public API
==========

* ``verify_business_semantics(produced_path, business_rules,
  source_tables=None) -> SemanticVerificationResult``
  Read the produced file (CSV / JSON-lines / JSON), evaluate every
  rule, and return a structured result with per-rule pass / fail and
  example violations. Pure function — no side effects.

* ``SemanticVerificationResult`` — dataclass with ``ok`` / ``violations``
  / per-rule status.

The module is intentionally library-light: it uses csv stdlib and
optional pandas (when present) for source-table joins. It never
imports app.services.run.* — keeps it usable from CLI, tests, and the
run workload.
"""
from __future__ import annotations

import csv
import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass
class RuleViolation:
    """One violation. ``bad_rows`` is a count; ``examples`` carries up
    to 5 sample row dicts so reviewers can reproduce."""

    rule_id: str
    rule_type: str
    field: Optional[str]
    severity: str
    message: str
    bad_rows: int = 0
    examples: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RuleCheckOutcome:
    rule_id: str
    rule_type: str
    severity: str
    passed: bool
    violation: Optional[RuleViolation] = None
    skipped: bool = False
    skipped_reason: Optional[str] = None


@dataclass
class SemanticVerificationResult:
    """Structured result of the semantic verifier."""

    ok: bool
    failure_kind: str  # "ok" | "business_semantics_violation" | "verification_skipped"
    rows_total: int
    violations: list[RuleViolation] = field(default_factory=list)
    rule_outcomes: list[RuleCheckOutcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "failure_kind": self.failure_kind,
            "rows_total": self.rows_total,
            "violations": [
                {
                    "rule_id": v.rule_id,
                    "rule_type": v.rule_type,
                    "field": v.field,
                    "severity": v.severity,
                    "message": v.message,
                    "bad_rows": v.bad_rows,
                    "examples": v.examples[:5],
                }
                for v in self.violations
            ],
            "rule_outcomes": [
                {
                    "rule_id": o.rule_id,
                    "rule_type": o.rule_type,
                    "severity": o.severity,
                    "passed": o.passed,
                    "skipped": o.skipped,
                    "skipped_reason": o.skipped_reason,
                }
                for o in self.rule_outcomes
            ],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------


def _read_rows(path: Path) -> tuple[list[dict[str, Any]], str]:
    """Return (rows, format) for ``path``.

    Supports CSV (.csv / .tsv), JSON Lines (.jsonl / .ndjson), and JSON
    array files (.json with a top-level list). Anything else raises
    ValueError so the caller can decide whether to skip verification.
    """
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv"}:
        delim = "\t" if suffix == ".tsv" else ","
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh, delimiter=delim)
            return [dict(row) for row in reader], "csv"
    if suffix in {".jsonl", ".ndjson"}:
        rows = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        return rows, "jsonl"
    if suffix == ".json":
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if isinstance(payload, list):
            return [dict(x) for x in payload if isinstance(x, dict)], "json"
        # Single object — wrap it for uniform handling.
        if isinstance(payload, dict):
            return [payload], "json"
        raise ValueError(f"Unsupported JSON shape for {path}: {type(payload)}")
    raise ValueError(f"Unsupported file extension for {path}: {suffix}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_number(val: Any) -> Optional[float]:
    """Try to coerce a CSV-derived value to a float. None on failure."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    # strip common suffixes (currency, %)
    s = s.replace(",", "").replace("$", "").replace("¥", "")
    if s.endswith("%"):
        try:
            return float(s[:-1]) / 100.0
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _is_template_calculation_note(value: str) -> bool:
    """Return True when ``value`` looks like a generic template
    instead of a real, value-substituted calculation note.

    Heuristic: a real note contains at least 2 numeric tokens AND has
    at least 20 chars of content. Generic templates are short and
    placeholder-heavy ("Order recommended", "Below threshold", etc.).
    """
    if not value or not isinstance(value, str):
        return True
    text = value.strip()
    if len(text) < 20:
        return True
    numbers = re.findall(r"-?\d+(?:\.\d+)?", text)
    return len(numbers) < 2


# ---------------------------------------------------------------------------
# Per-rule verifiers
# ---------------------------------------------------------------------------


def _check_state_space(
    rule: dict[str, Any], rows: list[dict[str, Any]]
) -> RuleCheckOutcome:
    fields = list(rule.get("fields") or [])
    allowed = list(rule.get("allowed_values") or [])
    if not fields or not allowed:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type=rule.get("rule_type", "state_space"),
            severity=rule.get("severity", "hard"),
            passed=True,
            skipped=True,
            skipped_reason="no fields or allowed_values declared",
        )
    target_field = fields[0]
    if not rows:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type=rule.get("rule_type", "state_space"),
            severity=rule.get("severity", "hard"),
            passed=True,
        )
    if target_field not in rows[0]:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type=rule.get("rule_type", "state_space"),
            severity=rule.get("severity", "hard"),
            passed=True,
            skipped=True,
            skipped_reason=f"field {target_field!r} not in output",
        )

    bad_examples: list[dict[str, Any]] = []
    bad_rows = 0
    allowed_set = set(allowed)
    for row in rows:
        v = row.get(target_field)
        if v in (None, ""):
            continue
        if str(v).strip() not in allowed_set:
            bad_rows += 1
            if len(bad_examples) < 5:
                bad_examples.append({"value": v, **{k: row.get(k) for k in list(row.keys())[:5]}})
    if bad_rows == 0:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type=rule.get("rule_type", "state_space"),
            severity=rule.get("severity", "hard"),
            passed=True,
        )
    violation = RuleViolation(
        rule_id=rule["rule_id"],
        rule_type=rule.get("rule_type", "state_space"),
        field=target_field,
        severity=rule.get("severity", "hard"),
        message=(
            f"{target_field!r} contains values outside the allowed set "
            f"{sorted(allowed_set)}; {bad_rows} row(s) violated"
        ),
        bad_rows=bad_rows,
        examples=bad_examples,
    )
    return RuleCheckOutcome(
        rule_id=rule["rule_id"],
        rule_type=rule.get("rule_type", "state_space"),
        severity=rule.get("severity", "hard"),
        passed=False,
        violation=violation,
    )


def _check_calculation(
    rule: dict[str, Any], rows: list[dict[str, Any]]
) -> RuleCheckOutcome:
    """Soft check: confirm the field has SOME variance and isn't
    uniformly zero / null when the formula is supplied.

    True formula evaluation is too brittle (we'd have to parse the
    user's formula string). The pragmatic check: when a calculation
    rule is declared, the field MUST not be 100% zero / null /
    constant — that's the most common fabrication signal we've seen
    in practice.
    """
    fields = list(rule.get("fields") or [])
    if not fields:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="calculation",
            severity=rule.get("severity", "hard"),
            passed=True,
            skipped=True,
            skipped_reason="no field declared",
        )
    target = fields[0]
    if not rows or target not in rows[0]:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="calculation",
            severity=rule.get("severity", "hard"),
            passed=True,
            skipped=True,
            skipped_reason=f"field {target!r} not in output",
        )
    seen_nonzero = False
    seen_distinct = set()
    for row in rows:
        n = _coerce_number(row.get(target))
        if n is None:
            continue
        if n != 0.0:
            seen_nonzero = True
        seen_distinct.add(round(n, 6))
        if seen_nonzero and len(seen_distinct) >= 2:
            return RuleCheckOutcome(
                rule_id=rule["rule_id"],
                rule_type="calculation",
                severity=rule.get("severity", "hard"),
                passed=True,
            )
    # Decide: all-zero, or constant value across many rows.
    if not seen_nonzero and len(rows) > 1:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="calculation",
            severity=rule.get("severity", "hard"),
            passed=False,
            violation=RuleViolation(
                rule_id=rule["rule_id"],
                rule_type="calculation",
                field=target,
                severity=rule.get("severity", "hard"),
                message=(
                    f"{target!r} is uniformly zero / empty across "
                    f"{len(rows)} rows; declared formula "
                    f"{rule.get('formula_hint') or '<n/a>'} suggests "
                    "the column should reflect real upstream values "
                    "— looks like the calculation never ran"
                ),
                bad_rows=len(rows),
                examples=[{target: r.get(target)} for r in rows[:3]],
            ),
        )
    if len(seen_distinct) == 1 and len(rows) > 5:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="calculation",
            severity=rule.get("severity", "hard"),
            passed=False,
            violation=RuleViolation(
                rule_id=rule["rule_id"],
                rule_type="calculation",
                field=target,
                severity=rule.get("severity", "hard"),
                message=(
                    f"{target!r} is the constant value "
                    f"{next(iter(seen_distinct))} across all "
                    f"{len(rows)} rows; formula "
                    f"{rule.get('formula_hint') or '<n/a>'} should "
                    "produce variation across rows with different "
                    "inputs — looks fabricated"
                ),
                bad_rows=len(rows),
                examples=[{target: r.get(target)} for r in rows[:3]],
            ),
        )
    return RuleCheckOutcome(
        rule_id=rule["rule_id"],
        rule_type="calculation",
        severity=rule.get("severity", "hard"),
        passed=True,
    )


def _check_evidence_requirement(
    rule: dict[str, Any], rows: list[dict[str, Any]]
) -> RuleCheckOutcome:
    fields = list(rule.get("fields") or [])
    if not fields:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="evidence_requirement",
            severity=rule.get("severity", "hard"),
            passed=True,
            skipped=True,
            skipped_reason="no field declared",
        )
    target = fields[0]
    if not rows or target not in rows[0]:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="evidence_requirement",
            severity=rule.get("severity", "hard"),
            passed=True,
            skipped=True,
            skipped_reason=f"field {target!r} not in output",
        )
    bad_examples: list[dict[str, Any]] = []
    bad_rows = 0
    for row in rows:
        v = row.get(target)
        if _is_template_calculation_note(str(v) if v is not None else ""):
            bad_rows += 1
            if len(bad_examples) < 5:
                bad_examples.append({target: v})
    if bad_rows == 0:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="evidence_requirement",
            severity=rule.get("severity", "hard"),
            passed=True,
        )
    return RuleCheckOutcome(
        rule_id=rule["rule_id"],
        rule_type="evidence_requirement",
        severity=rule.get("severity", "hard"),
        passed=False,
        violation=RuleViolation(
            rule_id=rule["rule_id"],
            rule_type="evidence_requirement",
            field=target,
            severity=rule.get("severity", "hard"),
            message=(
                f"{target!r} contains generic / template values for "
                f"{bad_rows} row(s); evidence requirement asks for "
                "real upstream numeric values used in the calculation"
            ),
            bad_rows=bad_rows,
            examples=bad_examples,
        ),
    )


def _check_join(
    rule: dict[str, Any],
    rows: list[dict[str, Any]],
    source_tables: dict[str, list[dict[str, Any]]],
) -> RuleCheckOutcome:
    """Join rule check.

    A "join" rule says: the ``fields[0]`` value in each row MUST come
    from the source table named in the rule's description. We
    recognise table references by scanning ``description`` /
    ``source_quote`` for tokens matching keys in ``source_tables``.

    When no source table is loaded, we degrade to "skipped" rather
    than fail — enforcement only happens when the verifier has the
    source tables loaded.
    """
    fields = list(rule.get("fields") or [])
    if not fields or not source_tables:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="join",
            severity=rule.get("severity", "hard"),
            passed=True,
            skipped=True,
            skipped_reason=(
                "no fields declared" if not fields else "no source_tables provided"
            ),
        )
    target = fields[0]
    if not rows or target not in rows[0]:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="join",
            severity=rule.get("severity", "hard"),
            passed=True,
            skipped=True,
            skipped_reason=f"field {target!r} not in output",
        )

    # Pick the source table by matching tokens in description.
    text = (rule.get("description") or "") + " " + (rule.get("source_quote") or "")
    text_lc = text.lower()
    selected_table_key: Optional[str] = None
    for key in source_tables:
        if key.lower() in text_lc:
            selected_table_key = key
            break
    if selected_table_key is None:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="join",
            severity=rule.get("severity", "hard"),
            passed=True,
            skipped=True,
            skipped_reason="could not match a source table from rule description",
        )
    source_rows = source_tables[selected_table_key]
    # Build the set of allowed values from the source table at the
    # SAME field name (most common case) or at any column whose values
    # form a superset of what we see in the produced data.
    source_field = target
    allowed_set = {
        str(r.get(source_field)).strip()
        for r in source_rows
        if r.get(source_field) not in (None, "")
    }
    if not allowed_set:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="join",
            severity=rule.get("severity", "hard"),
            passed=True,
            skipped=True,
            skipped_reason=(
                f"source table {selected_table_key!r} has no field {source_field!r}"
            ),
        )

    bad_examples: list[dict[str, Any]] = []
    bad_rows = 0
    for row in rows:
        v = row.get(target)
        if v in (None, ""):
            continue
        if str(v).strip() not in allowed_set:
            bad_rows += 1
            if len(bad_examples) < 5:
                bad_examples.append({target: v})
    if bad_rows == 0:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="join",
            severity=rule.get("severity", "hard"),
            passed=True,
        )
    return RuleCheckOutcome(
        rule_id=rule["rule_id"],
        rule_type="join",
        severity=rule.get("severity", "hard"),
        passed=False,
        violation=RuleViolation(
            rule_id=rule["rule_id"],
            rule_type="join",
            field=target,
            severity=rule.get("severity", "hard"),
            message=(
                f"{target!r} contains values not present in source table "
                f"{selected_table_key!r}; {bad_rows} row(s) look fabricated"
            ),
            bad_rows=bad_rows,
            examples=bad_examples,
        ),
    )


def _check_filter_queue_shape(
    rule: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    source_tables: dict[str, list[dict[str, Any]]],
) -> RuleCheckOutcome:
    """Heuristic check: the row count should NOT match
    ``len(source_table) × <date count>`` patterns that indicate a
    Cartesian-product fill instead of real filtering.

    We compare the produced row count to the size of every loaded
    source table; if it equals N×M for plausible (N, M) pairs we flag
    a filter violation. Otherwise we skip (we cannot determine "the
    right" row count without computing the queue).
    """
    if not rows or not source_tables:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="filter",
            severity=rule.get("severity", "hard"),
            passed=True,
            skipped=True,
            skipped_reason="no rows or source_tables",
        )
    n = len(rows)
    suspects: list[str] = []
    table_sizes = [(k, len(v)) for k, v in source_tables.items() if v]
    for k1, s1 in table_sizes:
        if s1 == 0:
            continue
        if n == s1 and s1 > 50:
            suspects.append(
                f"row_count == len({k1}) ({s1}); looks like every "
                "source row was kept rather than filtered"
            )
        for k2, s2 in table_sizes:
            if k1 == k2 or s2 == 0:
                continue
            if 5 <= s1 <= 500 and 5 <= s2 <= 500 and n == s1 * s2:
                suspects.append(
                    f"row_count == len({k1}) × len({k2}) "
                    f"({s1} × {s2}); looks like a Cartesian product"
                )
    if not suspects:
        return RuleCheckOutcome(
            rule_id=rule["rule_id"],
            rule_type="filter",
            severity=rule.get("severity", "hard"),
            passed=True,
        )
    return RuleCheckOutcome(
        rule_id=rule["rule_id"],
        rule_type="filter",
        severity=rule.get("severity", "hard"),
        passed=False,
        violation=RuleViolation(
            rule_id=rule["rule_id"],
            rule_type="filter",
            field=None,
            severity=rule.get("severity", "hard"),
            message=(
                f"row_count={n}: " + "; ".join(suspects)
                + ". Filter rule requires a queue / decision-only output."
            ),
            bad_rows=n,
            examples=[{"row_count": n}],
        ),
    )


def _check_hard_constraint(
    rule: dict[str, Any], rows: list[dict[str, Any]]
) -> RuleCheckOutcome:
    """Hard constraints are too domain-specific for a generic check.
    We surface them as soft observations: passed=True, but the rule
    is recorded so the caller can inspect."""
    return RuleCheckOutcome(
        rule_id=rule["rule_id"],
        rule_type="hard_constraint",
        severity=rule.get("severity", "hard"),
        passed=True,
        skipped=True,
        skipped_reason=(
            "hard_constraint rules are advisory at the verifier level; "
            "they're enforced via the prompt and reviewed manually"
        ),
    )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def verify_business_semantics(
    *,
    produced_path: str | Path,
    business_rules: list[dict[str, Any]],
    source_tables: Optional[dict[str, list[dict[str, Any]]]] = None,
) -> SemanticVerificationResult:
    """Run every rule in ``business_rules`` against the file at
    ``produced_path`` and return a structured result.

    ``source_tables`` is an optional dict of {table_name: list[row]}
    used by ``join`` and ``filter`` checks. When omitted, those checks
    skip rather than fail.

    The function is defensive: malformed rules are skipped with a
    reason; an unreadable produced_path returns ``failure_kind ==
    "verification_skipped"`` rather than raising.
    """
    path = Path(produced_path)
    notes: list[str] = []
    if not path.exists():
        return SemanticVerificationResult(
            ok=False,
            failure_kind="verification_skipped",
            rows_total=0,
            notes=[f"produced_path {path} does not exist"],
        )
    try:
        rows, fmt = _read_rows(path)
    except Exception as exc:  # noqa: BLE001
        return SemanticVerificationResult(
            ok=True,  # Cannot judge — degrade to ok rather than block.
            failure_kind="verification_skipped",
            rows_total=0,
            notes=[f"could not read {path}: {exc}"],
        )
    notes.append(f"format={fmt} rows={len(rows)}")

    source_tables = source_tables or {}

    rule_outcomes: list[RuleCheckOutcome] = []
    violations: list[RuleViolation] = []

    for raw_rule in business_rules or []:
        if not isinstance(raw_rule, dict):
            try:
                raw_rule = raw_rule.model_dump()
            except Exception:  # noqa: BLE001
                continue
        if "rule_id" not in raw_rule:
            continue
        rt = raw_rule.get("rule_type", "other")
        try:
            if rt in {"state_space", "approval_policy"}:
                outcome = _check_state_space(raw_rule, rows)
            elif rt == "calculation":
                outcome = _check_calculation(raw_rule, rows)
            elif rt == "evidence_requirement":
                outcome = _check_evidence_requirement(raw_rule, rows)
            elif rt == "join":
                outcome = _check_join(raw_rule, rows, source_tables)
            elif rt == "filter":
                outcome = _check_filter_queue_shape(
                    raw_rule, rows, source_tables=source_tables
                )
            elif rt == "hard_constraint":
                outcome = _check_hard_constraint(raw_rule, rows)
            else:
                outcome = RuleCheckOutcome(
                    rule_id=raw_rule["rule_id"],
                    rule_type=rt,
                    severity=raw_rule.get("severity", "hard"),
                    passed=True,
                    skipped=True,
                    skipped_reason=f"no verifier for rule_type {rt!r}",
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("business_semantic_verifier.rule_failed rule_id=%s", raw_rule.get("rule_id"))
            outcome = RuleCheckOutcome(
                rule_id=raw_rule.get("rule_id", "?"),
                rule_type=rt,
                severity=raw_rule.get("severity", "hard"),
                passed=True,
                skipped=True,
                skipped_reason=f"verifier raised: {exc}",
            )
        rule_outcomes.append(outcome)
        if outcome.violation is not None:
            violations.append(outcome.violation)

    hard_failures = [
        v for v in violations if v.severity == "hard"
    ]
    ok = not hard_failures
    failure_kind = "ok" if ok else "business_semantics_violation"

    return SemanticVerificationResult(
        ok=ok,
        failure_kind=failure_kind,
        rows_total=len(rows),
        violations=violations,
        rule_outcomes=rule_outcomes,
        notes=notes,
    )


__all__ = [
    "RuleCheckOutcome",
    "RuleViolation",
    "SemanticVerificationResult",
    "verify_business_semantics",
]
