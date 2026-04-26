"""Tests for SafeExprEngine — typed-node v0 foundation.

These tests are the audit boundary for everything that runs against
user data via formula_eval / classify_state_space / filter. Anything
permissive here propagates to those nodes — keep the surface tight.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.typed_nodes.expr_engine import (
    DOMAIN_FUNCTIONS,
    EvaluationError,
    NullInputError,
    SafeExprEngine,
    UnknownNameError,
    UnsafeExpressionError,
)


@pytest.fixture
def engine() -> SafeExprEngine:
    return SafeExprEngine()


# ---------------------------------------------------------------------------
# Parsing + AST whitelist (the most security-sensitive layer)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,reason",
    [
        # Imports / module access
        ("__import__('os')", "function call to dunder name"),
        # Lambdas
        ("(lambda x: x + 1)(5)", "Lambda node"),
        # Attribute access — the classic sandbox-escape vector
        ("().__class__.__bases__", "Attribute node"),
        ("x.foo", "Attribute node"),
        # Comprehensions
        ("[x for x in [1,2,3]]", "ListComp"),
        ("{x: y for x in [1] for y in [2]}", "DictComp"),
        ("{x for x in [1]}", "SetComp"),
        # Generators
        ("(x for x in [1,2])", "GeneratorExp"),
        # Walrus
        ("(x := 5)", "NamedExpr"),
        # Augmented / function defs (these don't even parse in eval mode
        # but we check anyway)
        # Subscripts (could be used to bypass attribute filter)
        ("x[0]", "Subscript"),
        ("x['key']", "Subscript"),
        # f-strings (use format() instead)
        ("f'hi {x}'", "JoinedStr"),
        # Slices
        ("x[1:2]", "Subscript"),
        # Empty / whitespace
        ("", "empty"),
        ("   ", "whitespace only"),
    ],
)
def test_static_check_rejects_unsafe_expressions(engine, expr, reason):
    with pytest.raises(UnsafeExpressionError):
        engine.referenced_names(expr)


@pytest.mark.parametrize(
    "expr",
    [
        "1 + 2",
        "a + b * c",
        "max(a, b) - min(c, d)",
        "x if y > 0 else 0",
        "x in [1, 2, 3]",
        "x not in (1, 2)",
        "(a > 0) and (b > 0) or not c",
        "round(qty * price, 2)",
        "ceil(qty / multiple) * multiple",
        "today_plus_days(7)",
        "format('hi {n}', n=name)",
        "coalesce(maybe_a, maybe_b, 'default')",
        "'static_string'",
        "True and not False",
    ],
)
def test_static_check_accepts_safe_expressions(engine, expr):
    # Provide a generous available set; we only care that parsing /
    # AST whitelist passes.
    available = {"a", "b", "c", "d", "x", "y", "qty", "price", "multiple",
                 "name", "maybe_a", "maybe_b"}
    engine.static_check(expr, available=available)


# ---------------------------------------------------------------------------
# Static check — column references
# ---------------------------------------------------------------------------


def test_static_check_returns_referenced_columns(engine):
    cols = engine.static_check(
        "max(a, b) + c * 2",
        available={"a", "b", "c", "d"},
    )
    assert cols == {"a", "b", "c"}


def test_static_check_excludes_function_names_from_column_set(engine):
    cols = engine.column_references("max(a, b) + ceil(c)")
    # max + ceil are reserved; only a, b, c are columns
    assert cols == {"a", "b", "c"}


def test_static_check_raises_on_unknown_column(engine):
    with pytest.raises(UnknownNameError) as exc:
        engine.static_check("a + missing_col", available={"a"})
    assert exc.value.name == "missing_col"
    assert "a" in exc.value.available


def test_referenced_names_includes_function_names(engine):
    # referenced_names is "everything", column_references filters
    names = engine.referenced_names("max(a, b)")
    assert "max" in names
    assert "a" in names
    assert "b" in names


# ---------------------------------------------------------------------------
# Evaluation — happy paths
# ---------------------------------------------------------------------------


def test_eval_basic_arithmetic(engine):
    assert engine.eval_row("a + b * 2", {"a": 1, "b": 3}) == 7


def test_eval_max_min_round(engine):
    assert engine.eval_row("max(a, b)", {"a": 5, "b": 3}) == 5
    assert engine.eval_row("min(a, b)", {"a": 5, "b": 3}) == 3
    assert engine.eval_row("round(qty * price, 2)",
                           {"qty": 3, "price": 1.236}) == 3.71


def test_eval_ceil_floor(engine):
    assert engine.eval_row("ceil(qty / 10) * 10",
                           {"qty": 23}) == 30
    assert engine.eval_row("floor(qty / 10) * 10",
                           {"qty": 23}) == 20


def test_eval_conditional(engine):
    out = engine.eval_row("x if x > 0 else 0", {"x": -5})
    assert out == 0
    out = engine.eval_row("x if x > 0 else 0", {"x": 5})
    assert out == 5


def test_eval_in_membership(engine):
    assert engine.eval_row(
        "lifecycle in ['EOL', 'Discontinued']",
        {"lifecycle": "EOL"},
    ) is True
    assert engine.eval_row(
        "lifecycle in ['EOL', 'Discontinued']",
        {"lifecycle": "Active"},
    ) is False


def test_eval_string_concat(engine):
    out = engine.eval_row(
        '"available=" + str(qty) + ", limit=" + str(lim)',
        {"qty": 8, "lim": 100},
    )
    assert out == "available=8, limit=100"


def test_eval_format_function(engine):
    out = engine.eval_row(
        "format('hi {name}, qty={q}', name=who, q=qty)",
        {"who": "valve", "qty": 10},
    )
    assert out == "hi valve, qty=10"


def test_eval_today_plus_days(engine):
    today = date.today()
    expected = (today + timedelta(days=7)).isoformat()
    assert engine.eval_row("today_plus_days(7)", {}) == expected


def test_eval_today_plus_days_with_column(engine):
    today = date.today()
    expected = (today + timedelta(days=12)).isoformat()
    assert engine.eval_row("today_plus_days(lead)", {"lead": 12}) == expected


def test_eval_coalesce(engine):
    assert engine.eval_row("coalesce(a, b, 'default')",
                           {"a": None, "b": None}) == "default"
    assert engine.eval_row("coalesce(a, b, 'default')",
                           {"a": None, "b": "x"}) == "x"


def test_eval_chained_assignment_via_caller(engine):
    """formula_eval will call eval_row with a row dict that grows
    (assignment B sees assignment A's target). Verify this works
    when caller pre-populates the row."""
    row = {"base": 10, "lead": 5}
    row["safety"] = engine.eval_row("base * lead * 2", row)
    row["reorder"] = engine.eval_row("safety + base * lead", row)
    assert row["safety"] == 100
    assert row["reorder"] == 150


# ---------------------------------------------------------------------------
# Evaluation — error paths
# ---------------------------------------------------------------------------


def test_eval_unknown_column_raises(engine):
    with pytest.raises(UnknownNameError):
        engine.eval_row("a + missing", {"a": 1})


def test_eval_division_by_zero_raises_evaluation_error(engine):
    with pytest.raises(EvaluationError) as exc:
        engine.eval_row("a / b", {"a": 1, "b": 0})
    assert "division_by_zero" in exc.value.cause


def test_eval_type_mismatch_raises_evaluation_error(engine):
    # Adding a string and an int → TypeError → EvaluationError
    with pytest.raises(EvaluationError):
        engine.eval_row("a + b", {"a": "hello", "b": 1})


# ---------------------------------------------------------------------------
# Null propagation
# ---------------------------------------------------------------------------


def test_eval_null_propagation_sql_returns_none(engine):
    out = engine.eval_row(
        "a + b * c", {"a": 1, "b": None, "c": 3},
        null_propagation="sql",
    )
    assert out is None


def test_eval_null_propagation_zero_substitutes(engine):
    out = engine.eval_row(
        "a + b", {"a": 5, "b": None},
        null_propagation="zero",
    )
    assert out == 5


def test_eval_null_propagation_raise(engine):
    with pytest.raises(NullInputError) as exc:
        engine.eval_row(
            "a + b", {"a": 5, "b": None},
            null_propagation="raise",
        )
    assert "b" in exc.value.null_columns


def test_eval_null_propagation_zero_treats_only_referenced_cols(engine):
    """Unreferenced columns being None must NOT trigger zero substitution."""
    out = engine.eval_row(
        "a + b",
        {"a": 5, "b": 3, "unrelated": None},
        null_propagation="zero",
    )
    assert out == 8


# ---------------------------------------------------------------------------
# Cache — hot loop performance
# ---------------------------------------------------------------------------


def test_parse_cache_reuses_tree(engine):
    """Calling referenced_names twice on the same expression should
    not re-parse — verify by checking the cache is populated."""
    expr = "a + b * 2"
    engine.referenced_names(expr)
    assert expr in engine._cache
    cached_entry = engine._cache[expr]
    engine.referenced_names(expr)
    assert engine._cache[expr] is cached_entry


# ---------------------------------------------------------------------------
# Domain function table — every function is wired
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fname,call_expr,row,expected",
    [
        ("max",     "max(a, b, c)",                {"a": 1, "b": 5, "c": 3}, 5),
        ("min",     "min(a, b, c)",                {"a": 1, "b": 5, "c": 3}, 1),
        ("abs",     "abs(x)",                       {"x": -7}, 7),
        ("round",   "round(x, 1)",                  {"x": 1.234}, 1.2),
        ("ceil",    "ceil(x)",                      {"x": 1.1}, 2),
        ("floor",   "floor(x)",                     {"x": 1.9}, 1),
        ("len",     "len(s)",                       {"s": "hello"}, 5),
        ("str",     "'x=' + str(v)",                {"v": 42}, "x=42"),
        ("coalesce","coalesce(a, b)",               {"a": None, "b": "fb"}, "fb"),
        ("format",  "format('{a}-{b}', a=x, b=y)",  {"x": 1, "y": 2}, "1-2"),
    ],
)
def test_domain_function_wired(engine, fname, call_expr, row, expected):
    assert fname in DOMAIN_FUNCTIONS
    assert engine.eval_row(call_expr, row) == expected


def test_function_not_in_domain_table_is_rejected(engine):
    """Functions not in DOMAIN_FUNCTIONS must be rejected even if
    referenced (e.g. `eval`, `__import__`, `print`)."""
    # ``eval`` is not in our table
    with pytest.raises((UnsafeExpressionError, EvaluationError, UnknownNameError)):
        engine.eval_row("eval('1+1')", {})


# ---------------------------------------------------------------------------
# Evil inputs — sandbox escape attempts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evil",
    [
        "__import__('os').system('rm -rf /')",
        "().__class__.__bases__[0].__subclasses__()",
        "x.__class__",
        "[1].__add__([2])",
        "open('/etc/passwd')",
        "globals()",
        "locals()",
        "exec('x = 1')",
        "compile('1', 'f', 'eval')",
    ],
)
def test_evil_inputs_rejected(engine, evil):
    """Sandbox-escape classics. All must fail before evaluation;
    most fail at the AST whitelist (Attribute / Subscript / Call to
    unknown function), the rest at simpleeval's name table."""
    with pytest.raises((UnsafeExpressionError, UnknownNameError, EvaluationError)):
        engine.eval_row(evil, {"x": 1})


# ---------------------------------------------------------------------------
# Real spare_parts-style expressions — end-to-end smoke
# ---------------------------------------------------------------------------


def test_spare_parts_safety_stock_formula(engine):
    """Realistic check from spare_parts catalog translation."""
    row = {
        "base_daily_demand": 4.8,
        "lead_time_days": 12,
        "criticality": "A",
    }
    expr = ("ceil(base_daily_demand * lead_time_days * "
            "(2.0 if criticality == 'A' else 1.5))")
    out = engine.eval_row(expr, row)
    # 4.8 * 12 * 2.0 = 115.2 → ceil = 116
    assert out == 116


def test_spare_parts_classify_predicate(engine):
    """A decision_list[*].when from classify_state_space."""
    row = {
        "available_qty": 8,
        "inbound_approved": 0,
        "reorder_point_internal": 153,
        "criticality": "A",
    }
    expr = ("(available_qty + inbound_approved) < reorder_point_internal "
            "and criticality == 'A'")
    assert engine.eval_row(expr, row) is True


def test_spare_parts_calculation_note_concat(engine):
    """Real calculation_note construction — uses string concat +
    str() function (decision (2) chose f-string later but this works
    today)."""
    row = {
        "available_qty": 8,
        "reorder_point_internal": 153,
        "target_stock_internal": 257,
        "lead_time_days": 12,
    }
    expr = (
        "'available=' + str(available_qty) + "
        "', reorder_point=' + str(reorder_point_internal) + "
        "', target=' + str(target_stock_internal) + "
        "', lead_time=' + str(lead_time_days) + 'd'"
    )
    out = engine.eval_row(expr, row)
    assert out == "available=8, reorder_point=153, target=257, lead_time=12d"
