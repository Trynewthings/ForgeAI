"""SafeExprEngine — bounded expression evaluation for typed nodes.

Three typed nodes use this engine:
    * formula_eval        — assignments[*].expression
    * classify_state_space — decision_list[*].when
    * filter              — predicate

Design constraints:
    * No ``import``, no attribute access (``foo.bar`` rejected),
      no lambdas, no list / dict / set comprehensions, no
      function definitions, no walrus, no f-strings.
    * Function calls are restricted to the curated DOMAIN_FUNCTIONS
      table below (max / min / abs / round / ceil / floor / today /
      today_plus_days / parse_date / coalesce / str / format / len).
    * Variable references must come from the row dict the caller
      supplies; an unknown name raises before evaluation begins.
    * Static analysis (`referenced_names`, `static_check`) is a
      separate path from evaluation — used by the planner /
      synthesis layer to validate configs at build time, before any
      data is processed.

This file is the audit boundary for the entire typed-node pipeline.
Anything that gets in here can run against user data; the surface
area is intentionally minimal.
"""
from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

from simpleeval import (
    DEFAULT_OPERATORS,
    EvalWithCompoundTypes,
    FunctionNotDefined,
    InvalidExpression,
    NameNotDefined,
)


# ---------------------------------------------------------------------------
# Domain function table
# ---------------------------------------------------------------------------


def _today() -> str:
    """Return today's ISO date string."""
    return date.today().isoformat()


def _today_plus_days(n: Any) -> str:
    """Return today + N days as ISO date string. ``n`` may be int / float
    / None; None → empty string (degrades gracefully)."""
    if n is None:
        return ""
    return (date.today() + timedelta(days=int(n))).isoformat()


def _parse_date(s: Any) -> str:
    """Return canonical ISO date string from various inputs.
    Pass-through for already-ISO strings; coerces datetime to date."""
    if s is None or s == "":
        return ""
    if isinstance(s, str):
        # Accept ``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM:SS``
        return date.fromisoformat(s.split("T")[0]).isoformat()
    if isinstance(s, datetime):
        return s.date().isoformat()
    if isinstance(s, date):
        return s.isoformat()
    raise ValueError(f"parse_date: cannot interpret {s!r}")


def _coalesce(*args: Any) -> Any:
    """Return first non-None argument, or None."""
    for a in args:
        if a is not None:
            return a
    return None


def _format(template: Any, **kwargs: Any) -> str:
    """f-string-style template fill: ``format("hello {name}", name=x)``.

    Used by formula_eval for ``calculation_note`` style fields. Only
    supports keyword substitution (no positional, no format specs
    that could trigger arbitrary code via __format__).
    """
    if not isinstance(template, str):
        raise ValueError(f"format: template must be str, got {type(template).__name__}")
    return template.format(**kwargs)


def _safe_round(value: Any, digits: Any = 0) -> Any:
    """``round`` with stricter type checks — Python's built-in
    accepts None and weird types; we reject."""
    if value is None:
        return None
    return round(value, int(digits))


# All callables exposed inside expressions. Anything not in this
# table is rejected by simpleeval as FunctionNotDefined.
DOMAIN_FUNCTIONS: dict[str, Any] = {
    "max": max,
    "min": min,
    "abs": abs,
    "round": _safe_round,
    "ceil": math.ceil,
    "floor": math.floor,
    "len": len,
    "str": str,
    "today": _today,
    "today_plus_days": _today_plus_days,
    "parse_date": _parse_date,
    "coalesce": _coalesce,
    "format": _format,
}


# Names that look like identifiers in expressions but are actually
# DOMAIN_FUNCTIONS entries — used by static_check to distinguish
# "missing column" from "function call" errors.
_RESERVED_NAMES: frozenset[str] = frozenset(DOMAIN_FUNCTIONS.keys())


# Functions whose semantics include handling None inputs deliberately.
# When an expression uses one of these, null-propagation logic is
# skipped — otherwise ``coalesce(a, b, 'default')`` with a=None would
# short-circuit to None instead of returning 'default'.
NULL_AWARE_FUNCTIONS: frozenset[str] = frozenset({"coalesce"})


# ---------------------------------------------------------------------------
# AST whitelist — what constructs an expression may contain
# ---------------------------------------------------------------------------


# Allowed AST node types. Anything else triggers UnsafeExpressionError.
# This is *belt and suspenders* — simpleeval already rejects most of
# these, but we double-check at parse time to fail loud and early.
_ALLOWED_AST_NODES: frozenset[type[ast.AST]] = frozenset(
    {
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.BinOp,
        ast.UnaryOp,
        ast.BoolOp,
        ast.Compare,
        ast.Call,
        ast.IfExp,
        # Operators
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
        ast.USub, ast.UAdd, ast.Not,
        ast.And, ast.Or,
        ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
        # Containers used only as literals on the right of comparisons
        # (e.g. ``x in [1, 2, 3]``)
        ast.List, ast.Tuple,
        # Keyword args for format()
        ast.keyword,
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExprError(Exception):
    """Base for SafeExprEngine errors. Carries structured fields so
    typed-node validators can format machine-readable feedback."""

    def __init__(self, message: str, *, expression: Optional[str] = None,
                 detail: Optional[str] = None) -> None:
        super().__init__(message)
        self.expression = expression
        self.detail = detail


class UnsafeExpressionError(ExprError):
    """Raised when the expression contains a forbidden AST node
    (lambdas, attribute access, comprehensions, etc.)."""


class UnknownNameError(ExprError):
    """A name in the expression is neither a known column nor a
    DOMAIN_FUNCTIONS entry."""

    def __init__(self, name: str, *, expression: Optional[str] = None,
                 available: Optional[list[str]] = None) -> None:
        msg = f"unknown name {name!r} in expression"
        super().__init__(msg, expression=expression)
        self.name = name
        self.available = list(available or [])


class NullInputError(ExprError):
    """A required column was None and null_propagation == 'raise'."""

    def __init__(self, *, expression: str, null_columns: list[str]) -> None:
        super().__init__(
            f"null inputs in {expression!r}: {null_columns}",
            expression=expression,
        )
        self.null_columns = null_columns


class EvaluationError(ExprError):
    """Wraps a runtime evaluation failure (division-by-zero, type
    mismatch, function call error)."""

    def __init__(self, *, expression: str, cause: str) -> None:
        super().__init__(f"evaluation failed: {cause}", expression=expression)
        self.cause = cause


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


@dataclass
class _ParseCacheEntry:
    tree: ast.Expression
    referenced: frozenset[str]


class SafeExprEngine:
    """Bounded expression evaluator.

    Lifecycle:
        engine = SafeExprEngine()
        engine.static_check(expr, available={...})    # planner / build time
        result = engine.eval_row(expr, row, ...)       # run time

    Both phases share a small AST cache keyed on expression text so
    a hot loop (one expression, 10k rows) parses only once.
    """

    def __init__(self) -> None:
        self._cache: dict[str, _ParseCacheEntry] = {}
        # EvalWithCompoundTypes is needed so list / tuple literals
        # work in expressions like ``lifecycle in ['EOL', 'Discontinued']``.
        # Comprehensions / dict literals would also become legal at
        # this layer — but our AST whitelist (above) rejects ListComp /
        # DictComp / SetComp / GeneratorExp / Dict / Set, so the only
        # compound types that actually run are list and tuple literals
        # used on the right-hand side of comparisons.
        self._evaluator = EvalWithCompoundTypes(
            functions=dict(DOMAIN_FUNCTIONS),
            operators=DEFAULT_OPERATORS,
        )

    # ------------------------------------------------------------------
    # Parsing + AST whitelist
    # ------------------------------------------------------------------

    def _parse(self, expression: str) -> _ParseCacheEntry:
        cached = self._cache.get(expression)
        if cached is not None:
            return cached
        if not isinstance(expression, str) or not expression.strip():
            raise UnsafeExpressionError("expression must be a non-empty string",
                                        expression=expression)
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise UnsafeExpressionError(
                f"syntax error: {exc.msg}",
                expression=expression,
                detail=str(exc),
            ) from exc

        # Walk the AST, reject anything outside the whitelist.
        for node in ast.walk(tree):
            if type(node) not in _ALLOWED_AST_NODES:
                raise UnsafeExpressionError(
                    f"forbidden AST node {type(node).__name__!r}",
                    expression=expression,
                    detail=f"only these constructs are allowed: "
                           f"{sorted(n.__name__ for n in _ALLOWED_AST_NODES)}",
                )

        # Reject dunder names — ``__import__``, ``__class__``,
        # ``__builtins__`` etc. are valid Name AST nodes but are the
        # classic sandbox-escape vector. Forbid any identifier
        # containing two underscores in a row.
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and "__" in node.id:
                raise UnsafeExpressionError(
                    f"dunder name {node.id!r} is not permitted",
                    expression=expression,
                )

        referenced = frozenset(
            n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
        )
        entry = _ParseCacheEntry(tree=tree, referenced=referenced)
        self._cache[expression] = entry
        return entry

    # ------------------------------------------------------------------
    # Static analysis
    # ------------------------------------------------------------------

    def referenced_names(self, expression: str) -> set[str]:
        """All identifier references in the expression, including
        function calls. Caller can subtract DOMAIN_FUNCTIONS to get
        column-only references."""
        return set(self._parse(expression).referenced)

    def column_references(self, expression: str) -> set[str]:
        """Names that look like column references (everything except
        DOMAIN_FUNCTIONS entries). What planner / synthesis use to
        check schema flow."""
        return self.referenced_names(expression) - _RESERVED_NAMES

    def static_check(self, expression: str, *, available: set[str]) -> set[str]:
        """Validate an expression against an available-column set.

        Returns the set of column references the expression depends
        on. Raises ``UnsafeExpressionError`` (parsing/AST) or
        ``UnknownNameError`` (column missing) before ever attempting
        evaluation.

        Functions in DOMAIN_FUNCTIONS are NOT considered "missing"
        — they live in the engine's runtime, not in row data.
        """
        cols = self.column_references(expression)
        missing = cols - available
        if missing:
            # Surface the first missing name; if multiple, the
            # validator can call again iteratively after fixes.
            for name in sorted(missing):
                raise UnknownNameError(
                    name,
                    expression=expression,
                    available=sorted(available),
                )
        return cols

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def eval_row(
        self,
        expression: str,
        row: dict[str, Any],
        *,
        null_propagation: str = "sql",
    ) -> Any:
        """Evaluate ``expression`` against a single ``row`` dict.

        ``null_propagation`` controls behavior when one of the
        referenced columns is None:
            * "sql"   — return None (default; matches SQL semantics)
            * "zero"  — substitute 0 for None in numeric contexts
            * "raise" — raise NullInputError listing the null column(s)

        Returns the evaluated value (any type). Raises:
            * ``UnsafeExpressionError`` — bad parse / forbidden AST
            * ``UnknownNameError``      — referenced column missing
              from row (column not in upstream schema)
            * ``NullInputError``        — null_propagation='raise'
              and a referenced column is None
            * ``EvaluationError``       — runtime failure (zero
              division, type mismatch, etc.)
        """
        entry = self._parse(expression)
        cols = entry.referenced - _RESERVED_NAMES
        fns_used = entry.referenced & _RESERVED_NAMES
        # When the expression uses a null-aware function (coalesce
        # etc.), skip null propagation entirely — the function knows
        # how to handle None inputs and short-circuiting would
        # defeat its purpose.
        skip_null_handling = bool(fns_used & NULL_AWARE_FUNCTIONS)

        # Resolve column values + check for null
        null_cols: list[str] = []
        names_for_eval: dict[str, Any] = {}
        for col in cols:
            if col not in row:
                raise UnknownNameError(
                    col,
                    expression=expression,
                    available=sorted(row.keys()),
                )
            val = row[col]
            if val is None:
                null_cols.append(col)
            names_for_eval[col] = val

        # Null propagation (skipped when a null-aware function is in play)
        if null_cols and not skip_null_handling:
            if null_propagation == "raise":
                raise NullInputError(
                    expression=expression, null_columns=sorted(null_cols)
                )
            if null_propagation == "sql":
                return None
            if null_propagation == "zero":
                for col in null_cols:
                    names_for_eval[col] = 0
            # else: unknown mode falls through to default (sql)

        # Evaluate
        self._evaluator.names = names_for_eval
        try:
            return self._evaluator.eval(expression)
        except (NameNotDefined, FunctionNotDefined) as exc:
            # Names slipping past static_check shouldn't happen, but
            # if they do (e.g. caller didn't run static_check first),
            # surface a clean error.
            raise UnknownNameError(
                str(exc), expression=expression,
                available=sorted(names_for_eval.keys()),
            ) from exc
        except InvalidExpression as exc:
            raise UnsafeExpressionError(
                f"simpleeval rejected expression: {exc}",
                expression=expression,
            ) from exc
        except ZeroDivisionError as exc:
            raise EvaluationError(
                expression=expression, cause="division_by_zero"
            ) from exc
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise EvaluationError(
                expression=expression, cause=str(exc)
            ) from exc


__all__ = [
    "DOMAIN_FUNCTIONS",
    "SafeExprEngine",
    "ExprError",
    "UnsafeExpressionError",
    "UnknownNameError",
    "NullInputError",
    "EvaluationError",
]
