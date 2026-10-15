"""
sql-coach-forge — Local-first SQL query coach.

Paste any SQL query (with optional flavor hint + free-form context like
"Snowflake on a 50M row events table, prod warehouse, daily batch").
Get back:
  - complexity tier (small / medium / large / expensive)
  - anti-pattern detection across ~30 patterns with severity + fix
  - execution plan pseudo-explain (scan / seek / join / sort / agg)
  - suggested indexes (when patterns imply them)
  - refactor suggestions (CTE rewrite, EXISTS vs IN, window fn swap, etc.)
  - 0-100 query-readiness score across 5 axes
  - flavor-aware hints (snowflake / bigquery / postgres / sqlserver / mysql / oracle / duckdb)
  - risk flags with severity high/medium/low and evidence quotes
  - REST API + SQLite session log + Markdown / JSON export. No LLM, no API keys.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from typing import Any

from flask import Flask, g, jsonify, request, send_from_directory

DB_PATH = os.environ.get("SQL_COACH_DB", os.path.join(os.path.dirname(__file__), "sql_coach.sqlite3"))

app = Flask(__name__, static_folder="static", static_url_path="")


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc: BaseException | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            flavor TEXT,
            context TEXT,
            query_text TEXT NOT NULL,
            complexity TEXT,
            score INTEGER,
            verdict TEXT,
            result_json TEXT NOT NULL,
            self_rating INTEGER,
            notes TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at);
        """
    )
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Tokenizer-ish helpers (intentionally lightweight — no full SQL parser)
# ---------------------------------------------------------------------------

WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
NUMBER_RE = re.compile(r"-?\b\d+(?:\.\d+)?\b")
STRING_RE = re.compile(r"'(?:[^']|'')*'")
LINE_COMMENT_RE = re.compile(r"--[^\n]*")
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def strip_comments(sql: str) -> str:
    sql = BLOCK_COMMENT_RE.sub(" ", sql)
    sql = LINE_COMMENT_RE.sub(" ", sql)
    return sql


def normalize(sql: str) -> str:
    sql = strip_comments(sql)
    sql = STRING_RE.sub("''", sql)
    # collapse whitespace
    sql = re.sub(r"\s+", " ", sql).strip()
    # pad parens / commas
    sql = re.sub(r"\(", " ( ", sql)
    sql = re.sub(r"\)", " ) ", sql)
    sql = re.sub(r",", " , ", sql)
    return sql


def tokens(sql: str) -> list[str]:
    return [t.lower() for t in WORD_RE.findall(sql)]


def count_keyword(tokens_list, kw: str) -> int:
    return sum(1 for t in tokens_list if t == kw.lower())


def word_count(sql: str) -> int:
    return len(WORD_RE.findall(sql))


def line_count(sql: str) -> int:
    return max(1, len([l for l in sql.splitlines() if l.strip()]))


def table_count(sql: str) -> int:
    """Rough count of unique FROM/JOIN table names."""
    tables: set[str] = set()
    # capture <token> after from/join (ignoring later reserved words like select/on/where)
    for m in re.finditer(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_\.]*)", sql, re.IGNORECASE):
        name = m.group(1).split(".")[-1].lower()
        if name not in RESERVED:
            tables.add(name)
    return len(tables)


RESERVED = {
    "select", "from", "where", "and", "or", "not", "in", "exists", "between",
    "is", "null", "like", "ilike", "as", "on", "case", "when", "then", "else",
    "end", "left", "right", "inner", "outer", "full", "cross", "join", "using",
    "group", "by", "order", "having", "limit", "offset", "fetch", "first", "next",
    "rows", "only", "with", "union", "intersect", "except", "all", "distinct",
    "asc", "desc", "nulls", "true", "false", "current_date", "current_timestamp",
}


# ---------------------------------------------------------------------------
# Flavor inference
# ---------------------------------------------------------------------------

FLAVORS = ["snowflake", "bigquery", "postgres", "postgresql", "sqlserver", "tsql", "mysql", "oracle", "duckdb", "redshift", "databricks", "spark"]


def detect_flavor(sql: str, context: str) -> str:
    blob = f"{sql}\n{context}".lower()
    # 1) Explicit mentions win first.
    for f in FLAVORS:
        if re.search(r"\b" + re.escape(f) + r"\b", blob):
            return f
    # 2) Order matters: more specific dialects before generic siblings.
    if re.search(r"\bqualify\b", blob):
        if re.search(r"\b(date_diff|datediff)\b", blob) or re.search(r"\bdelta\s+(lake|table)\b", blob):
            return "databricks"
        if re.search(r"\b(approx_count_distinct|any_value|struct|array_agg)\b", blob):
            return "bigquery"
        return "snowflake"
    if re.search(r"\bdate_diff\b", blob) or re.search(r"\bdelta\s+(lake|table)\b", blob):
        return "databricks"
    if re.search(r"\b(approx_count_distinct|any_value|struct)\b", blob):
        return "bigquery"
    if re.search(r"\b(date_trunc|daterange|jsonb_|::\s*\w+\s*|generate_series|interval\b)\b", blob):
        return "postgres"
    if re.search(r"\b(datediff|dateadd|charindex|isnull|top\s+\d+|with\s*\(\s*nolock\s*\))\b", blob):
        return "sqlserver"
    if re.search(r"\b(ifnull|group_concat|str_to_date|curdate)\b", blob):
        return "mysql"
    if re.search(r"\b(decode\(|rownum|connect\s+by|start\s+with)\b", blob):
        return "oracle"
    if re.search(r"\b(listagg|iff)\b", blob):
        return "snowflake"
    # NVL is shared by Snowflake & Oracle — Snowflake has iff(), Oracle has to_date/to_char/systdate/decode
    if re.search(r"\b(nvl|to_date|to_char|sysdate)\b", blob):
        # Prefer Oracle only when Oracle-specific signals are absent and Snowflake signals are absent
        if re.search(r"\b(listagg|iff)\b", blob):
            return "snowflake"
        if re.search(r"\b(decode\(|rownum|connect\s+by|start\s+with)\b", blob):
            return "oracle"
        # Default NVL → Oracle (NVL is the original; COALESCE is the standard)
        return "oracle"
    if re.search(r"\b(get_json_object|posexplode)\b", blob):
        return "spark"
    return "generic"


# ---------------------------------------------------------------------------
# Context parsing — row counts, frequency, prod-ness
# ---------------------------------------------------------------------------

ROW_HINT_RE = re.compile(r"(\d[\d,\.]*)\s*(m|million|b|billion|k|thousand|rows?)?", re.IGNORECASE)


def parse_context(context: str) -> dict[str, Any]:
    ctx = (context or "").lower()
    info: dict[str, Any] = {
        "raw": context or "",
        "row_count": None,
        "frequency": None,
        "is_prod": False,
        "is_warehouse": False,
        "is_adhoc": False,
        "mentioned_tables": [],
        "concerns": [],
    }
    if re.search(r"\b(prod|production|customer[- ]facing|critical)\b", ctx):
        info["is_prod"] = True
    if re.search(r"\b(warehouse|snowflake|bigquery|redshift|databricks|spark|fabric)\b", ctx):
        info["is_warehouse"] = True
    if re.search(r"\b(one[- ]off|ad[- ]?hoc|exploratory|investigat|debug|spot[- ]?check)\b", ctx):
        info["is_adhoc"] = True
    # Row count: prefer "X unit" patterns (50M / 1.2B / 200k / 100K rows)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(billion|b|million|m|k|thousand|rows?|records?)\b", ctx)
    if m:
        n = float(m.group(1))
        unit = m.group(2).lower()
        if unit.startswith("b"):
            info["row_count"] = int(n * 1_000_000_000)
        elif unit == "m" or unit == "million":
            info["row_count"] = int(n * 1_000_000)
        elif unit == "k" or unit == "thousand":
            info["row_count"] = int(n * 1_000)
        else:
            info["row_count"] = int(n)
    else:
        # fallback for bare numbers near "rows"
        m2 = re.search(r"(\d+)\s+rows?\b", ctx)
        if m2:
            info["row_count"] = int(m2.group(1))
    if re.search(r"\b(hourly|daily|every\s+\d+\s*(?:min|minute|hour|hr)|nightly|scheduled)\b", ctx):
        info["frequency"] = "scheduled"
    elif info["is_adhoc"]:
        info["frequency"] = "adhoc"
    else:
        info["frequency"] = "unknown"

    # Concerns: latency / cost / correctness / readability
    if re.search(r"\b(slow|latency|timeout|minutes|hours|takes\s+forever|hanging)\b", ctx):
        info["concerns"].append("performance")
    if re.search(r"\b(credit|cost|expensive|pricing|warehouse\s+size)\b", ctx):
        info["concerns"].append("cost")
    if re.search(r"\b(duplicate|wrong\s+count|mismatch|missing\s+rows|off\s+by)\b", ctx):
        info["concerns"].append("correctness")
    if re.search(r"\b(readable|review|onboarding|junior|hand[- ]?off)\b", ctx):
        info["concerns"].append("readability")

    # tables mentioned in context (rough): words ending in .csv, table=, or "on the X table"
    for m in re.finditer(r"\b(?:from|on|against|into|update)\s+(?:the\s+)?([a-z_][a-z0-9_]{2,})\b", ctx):
        info["mentioned_tables"].append(m.group(1))
    return info


# ---------------------------------------------------------------------------
# Anti-pattern library
# ---------------------------------------------------------------------------

# Each pattern: id, label, regex (against normalized sql), severity, axis_impact (which score axis),
# fix, description, evidence builder
ANTI_PATTERNS: list[dict[str, Any]] = [
    {
        "id": "select_star",
        "label": "SELECT *",
        "regex": r"\bselect\s+\*\b",
        "severity_when": "default_medium",
        "axis": "readability",
        "desc": "SELECT * pulls every column including future ones added to the schema, breaks downstream contracts, and prevents covering-index use.",
        "fix": "List the columns you actually need. If you truly need all columns, alias the table and use tbl.* explicitly.",
    },
    {
        "id": "select_star_count",
        "label": "SELECT * with COUNT",
        "regex": r"\bselect\s+count\s*\(\s*\*\s*\)\b",
        "severity_when": "default_low",
        "axis": "performance",
        "desc": "COUNT(*) is fine but if the goal is 'does a row exist', prefer EXISTS(...) which short-circuits at the first match.",
        "fix": "Use EXISTS(SELECT 1 FROM ...) for existence checks; reserve COUNT(*) for actual counts.",
    },
    {
        "id": "no_where_on_update_delete",
        "label": "UPDATE/DELETE without WHERE",
        "regex": r"\b(update|delete)\b[^;]*\b(set\s+|from\b)[\s\S]{0,400}?\bset\b[^;]{0,200}?(?=\bfrom\b|\bwhere\b|;|$)",
        "severity_when": "default_high",
        "axis": "correctness",
        "desc": "Caught the dangerous case: an UPDATE/DELETE without a clear WHERE filter will touch every row in the target table.",
        "fix": "Always include a WHERE clause. Run as SELECT first to preview affected row count.",
        "validator": "no_where_on_mutate",
    },
    {
        "id": "not_in_with_null",
        "label": "NOT IN with potentially-NULL list",
        "regex": r"\bnot\s+in\s*\(",
        "severity_when": "default_high",
        "axis": "correctness",
        "desc": "NOT IN returns no rows if the list contains a single NULL — a classic 'why is my query empty?' bug.",
        "fix": "Use NOT EXISTS (SELECT 1 FROM ... WHERE key = outer.key) or filter NULLs explicitly: WHERE col NOT IN (SELECT x FROM y WHERE x IS NOT NULL).",
    },
    {
        "id": "or_on_indexed_col",
        "label": "OR across indexed columns",
        "regex": r"\bwhere\b[\s\S]+?\b(\w+)\s*=\s*[^()]+?\bor\b\s*(\w+)\s*=",
        "severity_when": "default_medium",
        "axis": "performance",
        "desc": "OR on different columns frequently defeats index usage and falls back to scans, especially if one side is unindexed.",
        "fix": "Rewrite as UNION ALL of two indexed lookups, or restructure the logic so each branch can hit an index.",
    },
    {
        "id": "leading_wildcard_like",
        "label": "LIKE with leading wildcard",
        "regex": r"\blike\s+'%",
        "severity_when": "default_medium",
        "axis": "performance",
        "desc": "LIKE '%foo' forces a full table scan — no index can serve a leading wildcard.",
        "fix": "Use full-text search (tsvector on Postgres, SEARCH on BigQuery/Snowflake), a trigram index, or reverse-index the suffix.",
    },
    {
        "id": "ilike_leading_wildcard",
        "label": "ILIKE with leading wildcard",
        "regex": r"\bilike\s+'%",
        "severity_when": "default_medium",
        "axis": "performance",
        "desc": "ILIKE '%foo' is the case-insensitive version of the same trap.",
        "fix": "Same as LIKE — prefer full-text or trigram indexing.",
    },
    {
        "id": "function_on_column",
        "label": "Function wrapping indexed column",
        "regex": r"\bwhere\b[\s\S]+?\b(year|month|day|date_trunc|upper|lower|trim|cast|convert|substring|substr|to_char|to_date|date\()\s*\(\s*\w+",
        "severity_when": "default_medium",
        "axis": "performance",
        "desc": "Wrapping an indexed column in a function blocks index usage on most engines.",
        "fix": "Rewrite so the column is bare and the constant is transformed: WHERE created_at >= '2026-01-01' AND created_at < '2027-01-01' instead of YEAR(created_at) = 2026.",
    },
    {
        "id": "implicit_join",
        "label": "Old-style comma JOINs",
        "regex": r"\bfrom\s+(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|\w+)(?:\s+(?:as\s+)?\w+)?\s*,\s*(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|\w+)(?:\s+(?:as\s+)?\w+)?\s+\w+\s*,?\s*\w*\s+(?:where|on|inner|left|right|join|group|order|having|limit)\b",
        "severity_when": "default_low",
        "axis": "readability",
        "desc": "Comma joins are easy to misread — accidentally producing a Cartesian product when a JOIN condition is missing.",
        "fix": "Rewrite to explicit JOIN ... ON ... syntax with one join condition per join.",
    },
    {
        "id": "distinct_with_groupby",
        "label": "DISTINCT alongside GROUP BY",
        "regex": r"\bselect\s+distinct\b[\s\S]+?\bgroup\s+by\b",
        "severity_when": "default_low",
        "axis": "readability",
        "desc": "DISTINCT plus GROUP BY is usually a sign of confused intent — one of them is redundant.",
        "fix": "Pick one. GROUP BY is preferred when you actually want aggregates; DISTINCT is for unique-row projection.",
    },
    {
        "id": "order_by_no_limit",
        "label": "ORDER BY without LIMIT",
        "regex": r"\border\s+by\b(?![^;]*\blimit\b)(?![^;]*\bfetch\b)(?![^;]*\btop\s+\d+\b)",
        "severity_when": "default_low",
        "axis": "performance",
        "desc": "An unbounded ORDER BY forces a full sort of the result set, often spilling to disk on large tables.",
        "fix": "Add LIMIT n (or FETCH FIRST n ROWS ONLY). If you genuinely need everything sorted, sort in a downstream pipeline.",
    },
    {
        "id": "scalar_subquery_in_select",
        "label": "Correlated scalar subquery in SELECT",
        "regex": r"\bselect\b[\s\S]+?,\s*\(\s*select\b[\s\S]+?\bwhere\b[\s\S]+?\)\s*,?\s*\bfrom\b",
        "severity_when": "default_medium",
        "axis": "performance",
        "desc": "A scalar subquery in the SELECT list runs once per outer row — a classic N+1 hidden inside a single statement.",
        "fix": "Rewrite as a JOIN with aggregation, or use a window function over the joined set.",
    },
    {
        "id": "union_vs_union_all",
        "label": "UNION instead of UNION ALL",
        "regex": r"\bunion\b(?!\s+all\b)",
        "severity_when": "default_low",
        "axis": "performance",
        "desc": "Plain UNION de-duplicates the result (a global sort/hash), which is expensive when duplicates aren't actually possible.",
        "fix": "Use UNION ALL if duplicates are impossible or irrelevant. Keep UNION only when de-dup is the actual goal.",
    },
    {
        "id": "in_with_subquery",
        "label": "IN with large subquery",
        "regex": r"\b\w+\s+in\s*\(\s*select\b",
        "severity_when": "default_low",
        "axis": "readability",
        "desc": "IN (SELECT ...) is often a hidden anti-join with NULL pitfalls (see NOT IN). It's also less optimizable than EXISTS in many engines.",
        "fix": "Use EXISTS for semi-joins and NOT EXISTS for anti-joins. Reserve IN for short literal lists.",
    },
    {
        "id": "between_on_datetime",
        "label": "BETWEEN on datetime (off-by-one risk)",
        "regex": r"\b\w+\s+between\s+'?\d{4}-\d{2}-\d{2}",
        "severity_when": "default_medium",
        "axis": "correctness",
        "desc": "BETWEEN with two datetime endpoints is inclusive on both ends — this silently drops rows in the upper second (e.g. BETWEEN '2026-01-01' AND '2026-01-31' misses Jan 31 23:59:59).",
        "fix": "Use half-open ranges: WHERE ts >= '2026-01-01' AND ts < '2026-02-01'.",
    },
    {
        "id": "group_by_ordinal",
        "label": "GROUP BY ordinal position",
        "regex": r"\bgroup\s+by\s+\d",
        "severity_when": "default_low",
        "axis": "readability",
        "desc": "GROUP BY 1, 2 is brittle — it breaks the moment a column is added to the SELECT list.",
        "fix": "List the actual column names in GROUP BY.",
    },
    {
        "id": "select_constant_no_from",
        "label": "SELECT without FROM (potential bug)",
        "regex": r"^\s*select\s+(?!.*\bfrom\b)",
        "severity_when": "default_low",
        "axis": "readability",
        "desc": "Query has no FROM clause. If you intended to query a table, that's a bug. If you intended a constant, that's fine.",
        "fix": "Add a FROM if a table was meant; otherwise document that this is a constant expression.",
    },
    {
        "id": "join_without_on",
        "label": "JOIN missing ON/USING",
        "regex": r"\bjoin\s+\w+(?:\s+\w+)?(?!\s+(?:on|using))",
        "severity_when": "default_high",
        "axis": "correctness",
        "desc": "A JOIN without ON/USING produces a Cartesian product — the most common accidental-N² bug in SQL.",
        "fix": "Add ON <left.col> = <right.col> for every JOIN, or use CROSS JOIN explicitly if a Cartesian product is truly the goal.",
        "validator": "join_no_on",
    },
    {
        "id": "multiple_joins_same_table_no_alias",
        "label": "Same table joined twice without alias",
        "regex": r"\bjoin\s+(\w+)\b[\s\S]+?\bjoin\s+\1\b(?![\s\S]{0,40}\b(?:on|using)\s+\1\.)",
        "severity_when": "default_medium",
        "axis": "correctness",
        "desc": "Joining the same table twice without aliases will produce ambiguous columns and likely a parse error — or worse, a silent cross join.",
        "fix": "Alias both occurrences (e.g. JOIN users u JOIN users owner ON owner.id = u.owner_id) and qualify every column.",
    },
    {
        "id": "null_eq",
        "label": "NULL = NULL (always false)",
        "regex": r"(\b\w+\.\w+|\b\w+)\s*=\s*null\b|\bnull\s*=\s*(\b\w+\.\w+|\b\w+)",
        "severity_when": "default_high",
        "axis": "correctness",
        "desc": "= NULL is always NULL (falsy) — it does not test for nullness. A classic 'why are all my rows filtered?' trap.",
        "fix": "Use IS NULL / IS NOT NULL.",
    },
    {
        "id": "null_neq",
        "label": "NULL <> / != NULL",
        "regex": r"(\b\w+\.\w+|\b\w+)\s*(<>|!=)\s*null\b|\bnull\s*(<>|!=)\s*(\b\w+\.\w+|\b\w+)",
        "severity_when": "default_high",
        "axis": "correctness",
        "desc": "<> NULL / != NULL is also always NULL — it does NOT mean 'not null'.",
        "fix": "Use IS NOT NULL.",
    },
    {
        "id": "deeply_nested_subqueries",
        "label": "Subquery nesting ≥ 3",
        "regex": r"\(\s*select\b[\s\S]*?\(\s*select\b[\s\S]*?\(\s*select\b",
        "severity_when": "default_medium",
        "axis": "readability",
        "desc": "Three or more levels of nested subqueries are hard to review and hard for the planner to optimize.",
        "fix": "Refactor into CTEs (WITH cte1 AS (...)) so each step reads top-down.",
    },
    {
        "id": "case_when_no_else",
        "label": "CASE WHEN without ELSE",
        "regex": r"\bcase\s+when\b[\s\S]+?\bend\b(?![^;]*\belse\b)",
        "severity_when": "default_low",
        "axis": "correctness",
        "desc": "CASE without an ELSE branch silently returns NULL for unmatched rows — easy to miss in a long query.",
        "fix": "Add an explicit ELSE clause (even ELSE NULL) to make intent obvious.",
    },
    {
        "id": "division_by_possible_zero",
        "label": "Division by potentially-zero column",
        "regex": r"\b\w+\s*/\s*(\w+\.|\()?(\w+)",
        "severity_when": "default_medium",
        "axis": "correctness",
        "desc": "Dividing by a column value can return 'division by zero' errors at runtime if any row has 0/null.",
        "fix": "Use NULLIF(divisor, 0) in the denominator, or filter the rows first with a WHERE.",
    },
    {
        "id": "select_distinct_first_only",
        "label": "DISTINCT as perf crutch",
        "regex": r"\bselect\s+distinct\b[\s\S]+?\bjoin\b",
        "severity_when": "default_low",
        "axis": "correctness",
        "desc": "DISTINCT after JOINs usually means a join condition is wrong and producing duplicates.",
        "fix": "Fix the join keys instead of papering over the symptom with DISTINCT.",
    },
    {
        "id": "cast_in_where",
        "label": "CAST/CONVERT in WHERE",
        "regex": r"\bwhere\b[\s\S]+?\b(cast|convert)\s*\(\s*\w+\.",
        "severity_when": "default_medium",
        "axis": "performance",
        "desc": "CASTing an indexed column in WHERE prevents index use.",
        "fix": "Cast the literal instead: WHERE created_at >= CAST('2026-01-01' AS DATE).",
    },
    {
        "id": "or_on_diff_cols",
        "label": "OR between different columns",
        "regex": r"\bwhere\b[\s\S]+?\bor\b[\s\S]+?\bor\b",
        "severity_when": "default_low",
        "axis": "performance",
        "desc": "Three or more OR clauses typically defeat index usage entirely.",
        "fix": "Consider UNION ALL of indexed lookups, or full-text search if matching free text.",
    },
    {
        "id": "long_in_list",
        "label": "Very long IN list",
        "regex": r"\bin\s*\([^)]{200,}\)",
        "severity_when": "default_medium",
        "axis": "performance",
        "desc": "A long literal IN list (>200 chars) is usually a sign the list should be a JOIN to a small reference table or a temp table.",
        "fix": "Load the values into a staging/temp table and JOIN. Faster, easier to maintain, and parameterized cleanly.",
    },
    {
        "id": "no_alias_on_join",
        "label": "JOIN without alias",
        "regex": r"\bjoin\s+\w+(?:\s+(?:as\s+)?(?!\bon\b|\busing\b|\bcross\b|\binner\b|\bleft\b|\bright\b|\bfull\b))?\b\w*\b(?![\s\S]{0,30}\b(?:on|using)\b)",
        "severity_when": "default_low",
        "axis": "readability",
        "desc": "Joined tables without aliases force fully-qualified column names everywhere and obscure the join logic.",
        "fix": "Alias every joined table: JOIN orders o ON o.id = ....",
        "validator": "join_no_alias_strict",
    },
    {
        "id": "floating_point_money",
        "label": "Floating point on money",
        "regex": r"\b(cast|convert|\b)\s*\(?\s*[\w.]+\s+as\s+(float|double|double\s+precision|real|decimal\s*\(\s*\d+\s*,\s*\d+\s*\))",
        "severity_when": "default_low",
        "axis": "correctness",
        "desc": "If the value is money, prefer DECIMAL/NUMERIC with explicit precision; FLOAT accumulates rounding errors.",
        "fix": "CAST(col AS DECIMAL(18,2)) for currency.",
    },
]


# ---------------------------------------------------------------------------
# Risk detectors (extra, semantic)
# ---------------------------------------------------------------------------

def detect_join_no_on(sql: str) -> bool:
    """JOIN <table> not followed by ON/USING within ~30 chars."""
    for m in re.finditer(r"\b(?:join)\s+(\w+(?:\.\w+)?)\b", sql, re.IGNORECASE):
        after = sql[m.end():m.end() + 60]
        if not re.match(r"\s+(?:on|using|cross|inner|left|right|full|outer)\b", after, re.IGNORECASE):
            return True
    return False


def detect_no_where_on_mutate(sql: str) -> bool:
    """An UPDATE or DELETE statement (not in a CTE) without WHERE before ;"""
    # Look for top-level UPDATE / DELETE keyword
    m = re.search(r"\b(update|delete)\b\s+", sql, re.IGNORECASE)
    if not m:
        return False
    rest = sql[m.start():]
    # If rest is inside parens (subquery), skip
    open_parens = rest.count("(")
    close_parens = rest.count(")")
    if open_parens > close_parens:
        return False
    return not re.search(r"\bwhere\b", rest, re.IGNORECASE)


def detect_no_alias_on_join(sql: str) -> bool:
    """JOIN <table> without alias and without an ON keyword within ~30 chars."""
    for m in re.finditer(r"\bjoin\s+(\w+(?:\.\w+)?)\b", sql, re.IGNORECASE):
        tbl = m.group(1)
        after = sql[m.end():m.end() + 50]
        if re.match(r"\s+(?:on|using|cross|inner|left|right|full|outer)\b", after, re.IGNORECASE):
            continue
        # If the next token after the table is AS or another word (alias), fine
        m2 = re.match(r"\s+(?:as\s+)?(\w+)", after, re.IGNORECASE)
        if not m2:
            return True
        next_word = m2.group(1).lower()
        if next_word in {"on", "using", "where", "inner", "left", "right", "full", "cross", "outer", "join"}:
            continue
        # If the alias is just a reserved word like "the", that's also bad
        if next_word in {"a", "the", "an"}:
            return True
    return False


# ---------------------------------------------------------------------------
# Execution plan pseudo-explain
# ---------------------------------------------------------------------------

def pseudo_explain(sql: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    """Return an ordered list of plan steps with rationale, NOT real engine output."""
    steps: list[dict[str, Any]] = []
    tokens_list = tokens(sql)
    n_tables = table_count(sql)
    has_where = bool(re.search(r"\bwhere\b", sql, re.IGNORECASE))
    has_join = bool(re.search(r"\bjoin\b", sql, re.IGNORECASE))
    has_group = bool(re.search(r"\bgroup\s+by\b", sql, re.IGNORECASE))
    has_order = bool(re.search(r"\border\s+by\b", sql, re.IGNORECASE))
    has_limit = bool(re.search(r"\b(?:limit|fetch|top\s+\d+)\b", sql, re.IGNORECASE))
    has_distinct = bool(re.search(r"\bselect\s+distinct\b", sql, re.IGNORECASE))
    has_or = bool(re.search(r"\bor\b", sql, re.IGNORECASE))
    has_func_on_col = bool(re.search(r"\bwhere\b[\s\S]+?\b(year|month|day|upper|lower|trim|cast|convert|substring|substr|to_char|to_date|date_trunc)\s*\(\s*\w+\.", sql, re.IGNORECASE))
    has_leading_wildcard = bool(re.search(r"\blike\s+'%", sql, re.IGNORECASE))

    if has_leading_wildcard or has_func_on_col:
        steps.append({"op": "Seq Scan", "detail": "Forced scan — leading wildcard LIKE or function-on-column blocks index use.", "est_cost": "high"})
    elif has_where:
        steps.append({"op": "Index Seek / Filter", "detail": "WHERE on bare column may use index; rewrite any wrapped columns.", "est_cost": "medium"})
    else:
        steps.append({"op": "Full Table Scan", "detail": "No WHERE clause → full table scan on the smallest table in the FROM.", "est_cost": "high"})

    if has_join:
        if n_tables >= 4:
            steps.append({"op": "Hash Join (multi-way)", "detail": f"{n_tables} tables joined. Planner may pick Hash Joins; verify join order.", "est_cost": "high"})
        elif has_or:
            steps.append({"op": "Nested Loop / Hash", "detail": "OR clause may force Nested Loop with re-scans; consider UNION ALL.", "est_cost": "medium"})
        else:
            steps.append({"op": "Hash Join", "detail": "Equi-join with WHERE → Hash Join is the common plan.", "est_cost": "medium"})

    if has_group:
        steps.append({"op": "Hash Aggregate / Sort", "detail": "GROUP BY on many distinct values can spill to disk; check the cardinality.", "est_cost": "medium"})
    if has_distinct:
        steps.append({"op": "Deduplicate", "detail": "DISTINCT adds a sort/hash to remove duplicates. If the upstream already guarantees uniqueness, drop it.", "est_cost": "low"})
    if has_order and not has_limit:
        steps.append({"op": "Full Sort (no LIMIT)", "detail": "Unbounded ORDER BY sorts the entire result set; expensive on large tables.", "est_cost": "high"})
    elif has_order:
        steps.append({"op": "Top-N Sort", "detail": "ORDER BY with LIMIT can use a heap-sort and stop early.", "est_cost": "low"})

    # Final row estimate
    rows = context.get("row_count")
    if rows and rows >= 100_000_000:
        steps.append({"op": "⚠️  ~100M+ row scan", "detail": "Context suggests very large tables. Confirm partition pruning, clustering keys, and warehouse sizing.", "est_cost": "high"})
    elif rows and rows >= 1_000_000:
        steps.append({"op": "~1M+ row scan", "detail": "Multi-million row scan likely; consider clustering / partition filters.", "est_cost": "medium"})

    return steps


# ---------------------------------------------------------------------------
# Index suggestions
# ---------------------------------------------------------------------------

def suggest_indexes(sql: str) -> list[dict[str, str]]:
    suggestions: list[dict[str, str]] = []
    # Equality candidates: <col> = <value> in WHERE / JOIN ON
    eq_cols: list[str] = []
    for m in re.finditer(r"\b(\w+)\s*=\s*(?:\w+\.|\?|\w+)", sql):
        col = m.group(1).lower()
        if col not in RESERVED:
            eq_cols.append(col)
    seen: set[str] = set()
    for col in eq_cols:
        if col in seen:
            continue
        seen.add(col)
        suggestions.append({"column": col, "type": "btree", "rationale": "Equality predicate / join key — single-column B-tree will help unless distribution is highly skewed."})

    # ORDER BY column
    for m in re.finditer(r"\border\s+by\s+(?:\w+\.)?(\w+)", sql, re.IGNORECASE):
        col = m.group(1).lower()
        if col in RESERVED:
            continue
        suggestions.append({"column": col, "type": "btree / cluster key", "rationale": "ORDER BY column benefits from an index or, in warehouses, a clustering key."})

    # LIKE patterns (only trailing wildcards can use an index)
    for m in re.finditer(r"\b\w+\s+like\s+'([^']+)'", sql, re.IGNORECASE):
        pat = m.group(1)
        if pat and not pat.startswith("%"):
            suggestions.append({"column": "(text column)", "type": "btree / n-gram / FTS", "rationale": f"Trailing-wildcard pattern {pat!r} can use a B-tree prefix; for prefix LIKE patterns consider full-text search or a trigram index."})
            break

    return suggestions


# ---------------------------------------------------------------------------
# Refactor suggestions
# ---------------------------------------------------------------------------

REFACTORS = [
    {
        "id": "exists_over_in",
        "trigger": "in_with_subquery",
        "label": "Convert IN-subquery to EXISTS",
        "detail": "EXISTS short-circuits at the first match and avoids the NOT IN + NULL trap. Especially important when nullability is uncertain.",
        "example_before": "select count(*) from orders o where o.user_id in (select id from users where country = 'US')",
        "example_after": "select count(*) from orders o where exists (select 1 from users u where u.id = o.user_id and u.country = 'US')",
    },
    {
        "id": "not_exists_over_not_in",
        "trigger": "not_in_with_null",
        "label": "Convert NOT IN to NOT EXISTS",
        "detail": "NOT IN silently returns zero rows when the list contains NULL. NOT EXISTS is NULL-safe and almost always what you meant.",
        "example_before": "select * from users where id not in (select user_id from banned)",
        "example_after": "select * from users u where not exists (select 1 from banned b where b.user_id = u.id)",
    },
    {
        "id": "union_all_over_union",
        "trigger": "union_vs_union_all",
        "label": "UNION → UNION ALL (when dedup is irrelevant)",
        "detail": "UNION ALL skips the global dedup sort/hash. Use it whenever the inputs are already distinct or you don't care about duplicates.",
        "example_before": "select id from a union select id from b",
        "example_after": "select id from a union all select id from b",
    },
    {
        "id": "half_open_range",
        "trigger": "between_on_datetime",
        "label": "BETWEEN datetime → half-open range",
        "detail": "BETWEEN is inclusive on both endpoints; a same-day upper bound silently drops the last second. Use ts >= start AND ts < end.",
        "example_before": "where created_at between '2026-01-01' and '2026-01-31'",
        "example_after": "where created_at >= '2026-01-01' and created_at < '2026-02-01'",
    },
    {
        "id": "cte_for_nesting",
        "trigger": "deeply_nested_subqueries",
        "label": "Refactor nested subqueries to CTEs",
        "detail": "WITH cte AS (...) reads top-down and is far easier to review, lint, and explain.",
        "example_before": "select * from (select * from (select * from events where ...)) where ...",
        "example_after": "with e as (select * from events where ...), f as (select * from e where ...) select * from f",
    },
    {
        "id": "scalar_to_window",
        "trigger": "scalar_subquery_in_select",
        "label": "Move scalar subquery to a window/join",
        "detail": "A scalar subquery in the SELECT list runs per outer row (effectively N+1). JOIN + GROUP BY or a window function processes once.",
        "example_before": "select u.id, (select max(o.created_at) from orders o where o.user_id = u.id) as last_order from users u",
        "example_after": "select u.id, max(o.created_at) as last_order from users u left join orders o on o.user_id = u.id group by u.id",
    },
    {
        "id": "is_null",
        "trigger": "null_eq",
        "label": "Use IS NULL / IS NOT NULL",
        "detail": "= NULL and <> NULL are always NULL (falsy), never true. Use IS NULL / IS NOT NULL.",
        "example_before": "where deleted_at = null",
        "example_after": "where deleted_at is null",
    },
    {
        "id": "nullif_for_div",
        "trigger": "division_by_possible_zero",
        "label": "NULLIF for divide-by-zero safety",
        "detail": "Wrap the denominator in NULLIF(divisor, 0) so a zero becomes NULL instead of an exception.",
        "example_before": "select amount / units as per_unit from sales",
        "example_after": "select amount / nullif(units, 0) as per_unit from sales",
    },
]


# ---------------------------------------------------------------------------
# Flavor-specific hints
# ---------------------------------------------------------------------------

FLAVOR_HINTS: dict[str, list[str]] = {
    "snowflake": [
        "QUALIFY is your friend for window-function filtering — push WHERE-on-window into QUALIFY when possible.",
        "Avoid SELECT *. Snowflake charges by bytes scanned, so a SELECT * on a wide table is the most expensive mistake.",
        "Clustering keys help on very large tables; pick them on the columns you most often filter or join on.",
        "DATE_TRUNC('day', col) is sargable-friendly when the literal side is cast; wrapping the column is not.",
    ],
    "bigquery": [
        "BigQuery is columnar — SELECT * is genuinely the #1 cost trap; you pay per column scanned.",
        "Use partition pruning (WHERE _PARTITIONDATE = ...) on any time-partitioned table before filtering further.",
        "Use APPROX_COUNT_DISTINCT instead of COUNT(DISTINCT) on billion-row tables for ~1% error with much lower cost.",
        "Window functions inside a single query are often cheaper than correlated scalar subqueries.",
    ],
    "postgres": [
        "EXPLAIN (ANALYZE, BUFFERS) is the ground truth — read the actual plan, not the estimated one.",
        "Prefer EXISTS over IN for correlated subqueries; PG often rewrites IN to a semi-join automatically, but EXISTS is clearer.",
        "Use partial indexes (CREATE INDEX ... WHERE ...) for hot-path filtered lookups (e.g. active = true).",
        "For LIKE 'foo%' use a B-tree on the column; for '%foo%' use pg_trgm GIN.",
    ],
    "sqlserver": [
        "Include actual execution plans (Ctrl+L) when reviewing; estimated plans hide parameter-sniffing traps.",
        "Watch for implicit conversions (VARCHAR compared to NVARCHAR column) — they cause index scans.",
        "OPTION (RECOMPILE) helps when parameter sniffing causes wildly different plans for different inputs.",
        "Prefer SET-BASED logic over cursors/whiles; SQL Server excels at set operations.",
    ],
    "mysql": [
        "EXPLAIN FORMAT=JSON gives you the cost-based plan; classic EXPLAIN is sometimes misleading.",
        "Index column order matters — leftmost prefix rule means composite indexes only help left-to-right lookups.",
        "For MySQL 8+, use window functions instead of session variables for running totals.",
        "Avoid SELECT * with TEXT/BLOB columns — they're stored off-page and slow everything down.",
    ],
    "oracle": [
        "Use SQL Trace + tkprof or DBMS_XPLAN.DISPLAY_CURSOR for real plans, not EXPLAIN PLAN.",
        "NVL vs COALESCE: NVL is Oracle-specific and slightly faster for two operands; COALESCE is ANSI.",
        "Bind variable peeking can cause wildly different plans; consider adaptive cursor sharing or histograms.",
    ],
    "duckdb": [
        "DuckDB is columnar and in-process; SELECT * is cheap but still slower than listing columns when projecting.",
        "Use SAMPLE to preview large query results quickly.",
        "LIST and STRUCT aggregations are first-class — useful for JSON-shaped outputs.",
    ],
    "databricks": [
        "Photon accelerates SELECT, JOIN, AGG, and window functions; scalar UDFs disable Photon.",
        "Use Adaptive Query Execution (AQE) — it coalesces partitions and switches join strategies at runtime.",
        "Delta Lake's data skipping + Z-ordering helps when WHERE filters on the Z-ordered columns.",
    ],
    "spark": [
        "Avoid UDFs that aren't Pandas UDFs — they break Catalyst optimizations and trigger row-by-row execution.",
        "Use repartition() strategically; too many small files or too few large files both hurt.",
        "Predicate pushdown only works on file formats that support it (Parquet, ORC with partition columns).",
    ],
    "redshift": [
        "Sort keys (compound or interleaved) act as clustered indexes — pick the WHERE / JOIN columns.",
        "Distribution style matters — KEY on the join column to colocate, ALL for small dimension tables.",
        "Avoid functions on sort-key columns in WHERE; same as worst case for any engine.",
    ],
    "generic": [
        "Run EXPLAIN on the real plan (most engines have a 'real' plan command — that's the truth).",
        "Indexes are not free — each one slows writes; review before adding more.",
    ],
}


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze(query: str, context: str) -> dict[str, Any]:
    norm = normalize(query)
    toks = tokens(norm)
    ctx = parse_context(context)

    flavor = detect_flavor(norm, context)
    n_tables = table_count(norm)
    n_words = word_count(query)

    # ---- anti-pattern detection ----
    # Match against the original query (with comments stripped but strings preserved)
    # so pattern-string contents like LIKE '%x' aren't lost.
    raw_no_comments = strip_comments(query)
    findings: list[dict[str, Any]] = []
    axis_hits: dict[str, int] = {"correctness": 0, "performance": 0, "readability": 0, "portability": 0}
    for p in ANTI_PATTERNS:
        if re.search(p["regex"], raw_no_comments, re.IGNORECASE):
            # validators
            if p.get("validator") == "no_where_on_mutate" and not detect_no_where_on_mutate(raw_no_comments):
                continue
            if p.get("validator") == "join_no_on" and not detect_join_no_on(raw_no_comments):
                continue
            if p.get("validator") == "join_no_alias_strict" and not detect_no_alias_on_join(raw_no_comments):
                continue

            sev = p["severity_when"].replace("default_", "")
            # Adjust severity by context
            if p["id"] == "select_star" and ctx.get("is_adhoc"):
                sev = "low"
            if p["id"] == "order_by_no_limit" and ctx.get("is_adhoc"):
                sev = "low"
            if p["id"] == "union_vs_union_all" and ctx.get("frequency") == "adhoc":
                sev = "low"
            if p["id"] in ("select_star", "order_by_no_limit", "union_vs_union_all") and ctx.get("frequency") == "scheduled" and ctx.get("is_prod"):
                sev = "high" if sev != "high" else "high"

            severity_weight = {"high": 22, "medium": 14, "low": 6}.get(sev, 4)
            if ctx.get("is_prod"):
                severity_weight = int(severity_weight * 1.25)

            findings.append({
                "id": p["id"],
                "label": p["label"],
                "severity": sev,
                "severity_weight": severity_weight,
                "axis": p["axis"],
                "description": p["desc"],
                "fix": p["fix"],
                "evidence": _evidence_snippet(query, p["regex"]),
            })
            if sev == "high":
                axis_hits[p["axis"]] += 12
            elif sev == "medium":
                axis_hits[p["axis"]] += 7
            else:
                axis_hits[p["axis"]] += 3

    # ---- complexity tier ----
    score_signals = 0
    score_signals += n_tables * 2
    score_signals += max(0, (n_words - 80) // 30) * 2
    score_signals += count_keyword(toks, "join") * 2
    score_signals += count_keyword(toks, "or") * 2
    score_signals += count_keyword(toks, "subquery".lower()) * 0  # not a keyword; leave
    score_signals += count_keyword(toks, "select") * 0
    score_signals += (count_keyword(toks, "case") + count_keyword(toks, "when")) // 2
    # row_count pressure
    rc = ctx.get("row_count")
    if rc:
        if rc >= 100_000_000:
            score_signals += 20
        elif rc >= 10_000_000:
            score_signals += 12
        elif rc >= 1_000_000:
            score_signals += 6
        elif rc >= 100_000:
            score_signals += 3
    if ctx.get("is_prod"):
        score_signals += 5

    if score_signals >= 30:
        complexity = "expensive"
    elif score_signals >= 18:
        complexity = "large"
    elif score_signals >= 8:
        complexity = "medium"
    else:
        complexity = "small"

    # ---- execution plan ----
    plan = pseudo_explain(norm, ctx)

    # ---- index suggestions ----
    indexes = suggest_indexes(norm)

    # ---- refactors ----
    triggered_ids = {f["id"] for f in findings}
    refactors = []
    for r in REFACTORS:
        if r["trigger"] in triggered_ids:
            refactors.append({
                "id": r["id"],
                "label": r["label"],
                "detail": r["detail"],
                "example_before": r["example_before"],
                "example_after": r["example_after"],
            })

    # ---- flavor hints ----
    flavor_hints = FLAVOR_HINTS.get(flavor, FLAVOR_HINTS["generic"])

    # ---- scoring ----
    # Start at 100, subtract based on findings
    correctness_penalty = sum(f["severity_weight"] for f in findings if f["axis"] == "correctness")
    performance_penalty = sum(f["severity_weight"] for f in findings if f["axis"] == "performance")
    readability_penalty = sum(f["severity_weight"] for f in findings if f["axis"] == "readability")
    portability_penalty = sum(f["severity_weight"] for f in findings if f["axis"] == "portability")

    correctness = max(0, 100 - correctness_penalty)
    performance = max(0, 100 - performance_penalty)
    readability = max(0, 100 - readability_penalty)
    portability = max(0, 100 - portability_penalty)
    # bonus axes
    structure = 100 - (5 if detect_no_where_on_mutate(norm) else 0) - (5 if detect_join_no_on(norm) else 0)
    structure -= min(15, max(0, (n_tables - 4)) * 3)  # penalize excessive joins
    structure -= min(10, max(0, (n_words - 200) // 50) * 2)
    structure -= min(15, max(0, (len(findings) - 3) * 2))  # many findings → messy
    structure = max(0, structure)

    # overall: weighted; bias toward correctness when prod
    weights = {"correctness": 0.30, "performance": 0.30, "readability": 0.15, "structure": 0.10, "portability": 0.15}
    if ctx.get("is_prod"):
        weights = {"correctness": 0.35, "performance": 0.35, "readability": 0.10, "structure": 0.10, "portability": 0.10}
    if ctx.get("is_adhoc"):
        weights = {"correctness": 0.30, "performance": 0.15, "readability": 0.20, "structure": 0.15, "portability": 0.20}
    overall = round(
        correctness * weights["correctness"]
        + performance * weights["performance"]
        + readability * weights["readability"]
        + structure * weights["structure"]
        + portability * weights["portability"]
    )
    # Dampen by total finding load: a query with N findings shouldn't sit at "ship-ready"
    if len(findings) >= 6:
        overall -= 10
    if len(findings) >= 4:
        overall -= 5
    if any(f["severity"] == "high" for f in findings) and ctx.get("is_prod"):
        overall -= 5
    overall = max(0, min(100, overall))

    if overall >= 85:
        verdict = "ship_ready"
    elif overall >= 70:
        verdict = "mostly_ready"
    elif overall >= 55:
        verdict = "needs_polish"
    elif overall >= 40:
        verdict = "tight_but_usable"
    else:
        verdict = "needs_rework"

    # ---- tip ----
    if findings:
        top = sorted(findings, key=lambda f: {"high": 3, "medium": 2, "low": 1}[f["severity"]], reverse=True)[0]
        tip = f"Top fix: {top['label']} ({top['severity']}). {top['fix']}"
    elif complexity in ("expensive", "large"):
        tip = "Looks syntactically clean — focus on the execution plan and warehouse sizing for the prod path."
    else:
        tip = "Looks solid. Re-run after adding the real schema / row counts to catch warehouse-specific traps."

    return {
        "complexity": complexity,
        "complexity_score": score_signals,
        "tables": n_tables,
        "word_count": n_words,
        "flavor": flavor,
        "context_summary": {
            "row_count": ctx.get("row_count"),
            "frequency": ctx.get("frequency"),
            "is_prod": ctx.get("is_prod"),
            "is_warehouse": ctx.get("is_warehouse"),
            "concerns": ctx.get("concerns") or [],
        },
        "findings": findings,
        "plan": plan,
        "indexes": indexes,
        "refactors": refactors,
        "flavor_hints": flavor_hints,
        "scores": {
            "correctness": correctness,
            "performance": performance,
            "readability": readability,
            "structure": structure,
            "portability": portability,
            "overall": overall,
        },
        "verdict": verdict,
        "tip": tip,
    }


def _evidence_snippet(query: str, pattern: str) -> str:
    m = re.search(pattern, query, re.IGNORECASE)
    if not m:
        return ""
    start = max(0, m.start() - 30)
    end = min(len(query), m.end() + 30)
    snippet = query[start:end].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(query) else "")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index() -> Any:
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/health")
def health() -> Any:
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat() + "Z"})


@app.route("/api/analyze", methods=["POST"])
def api_analyze() -> Any:
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    context = (data.get("context") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    if len(query) > 50_000:
        return jsonify({"error": "query too long (50k char cap)"}), 400
    result = analyze(query, context)
    return jsonify(result)


@app.route("/api/sessions", methods=["GET"])
def list_sessions() -> Any:
    db = get_db()
    rows = db.execute(
        "SELECT id, created_at, flavor, complexity, score, verdict, self_rating FROM sessions ORDER BY id DESC LIMIT 100"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/sessions", methods=["POST"])
def create_session() -> Any:
    data = request.get_json(force=True, silent=True) or {}
    query = (data.get("query") or "").strip()
    context = (data.get("context") or "").strip()
    result = data.get("result") or {}
    if not query or not result:
        return jsonify({"error": "query and result required"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO sessions (created_at, flavor, context, query_text, complexity, score, verdict, result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.utcnow().isoformat() + "Z",
            result.get("flavor"),
            context,
            query,
            result.get("complexity"),
            (result.get("scores") or {}).get("overall"),
            result.get("verdict"),
            json.dumps(result),
        ),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid})


@app.route("/api/sessions/<int:sid>", methods=["GET"])
def get_session(sid: int) -> Any:
    db = get_db()
    row = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    d = dict(row)
    d["result"] = json.loads(d.pop("result_json"))
    return jsonify(d)


@app.route("/api/sessions/<int:sid>", methods=["PATCH"])
def patch_session(sid: int) -> Any:
    data = request.get_json(force=True, silent=True) or {}
    self_rating = data.get("self_rating")
    notes = data.get("notes")
    db = get_db()
    row = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    if self_rating is not None and (self_rating < 1 or self_rating > 5):
        return jsonify({"error": "self_rating must be 1-5"}), 400
    db.execute(
        "UPDATE sessions SET self_rating = COALESCE(?, self_rating), notes = COALESCE(?, notes) WHERE id = ?",
        (self_rating, notes, sid),
    )
    db.commit()
    return get_session(sid)


@app.route("/api/export/<int:sid>")
def export_session(sid: int) -> Any:
    fmt = request.args.get("fmt", "md").lower()
    db = get_db()
    row = db.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()
    if not row:
        return jsonify({"error": "not found"}), 404
    d = dict(row)
    res = json.loads(d["result_json"])
    if fmt == "json":
        return jsonify({"session": d, "result": res})
    # Markdown
    md = []
    md.append(f"# SQL Coach — Session #{sid}\n")
    md.append(f"**Created:** {d['created_at']}  ")
    md.append(f"**Flavor:** {d['flavor']}  ")
    md.append(f"**Complexity:** {d['complexity']}  ")
    md.append(f"**Verdict:** {d['verdict']}  ")
    md.append(f"**Overall score:** {d['score']}/100\n")
    if d.get("context"):
        md.append(f"**Context:** {d['context']}\n")
    md.append("\n## Scores\n")
    for k, v in (res.get("scores") or {}).items():
        md.append(f"- **{k}**: {v}")
    md.append(f"\n## Top tip\n{res.get('tip','')}\n")
    md.append("\n## Findings\n")
    for f in res.get("findings", []):
        md.append(f"### {f['label']} — {f['severity']}")
        md.append(f"{f['description']}")
        if f.get("evidence"):
            md.append(f"> Evidence: `{f['evidence']}`")
        md.append(f"**Fix:** {f['fix']}\n")
    md.append("\n## Execution plan (pseudo)\n")
    for p in res.get("plan", []):
        md.append(f"- **{p['op']}** ({p['est_cost']}): {p['detail']}")
    md.append("\n## Suggested indexes\n")
    for ix in res.get("indexes", []):
        md.append(f"- `{ix['column']}` ({ix['type']}): {ix['rationale']}")
    md.append("\n## Refactors\n")
    for r in res.get("refactors", []):
        md.append(f"### {r['label']}\n{r['detail']}\n")
        md.append(f"**Before**\n```sql\n{r['example_before']}\n```\n")
        md.append(f"**After**\n```sql\n{r['example_after']}\n```\n")
    md.append("\n## Flavor hints ({})\n".format(d["flavor"]))
    for h in res.get("flavor_hints", []):
        md.append(f"- {h}")
    if d.get("self_rating"):
        md.append(f"\n## Self-rating\n**{d['self_rating']} / 5**\n{d.get('notes','')}")
    return jsonify({"markdown": "\n".join(md)})


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

@app.route("/<path:path>")
def static_proxy(path: str) -> Any:
    return send_from_directory(app.static_folder, path)


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5001, debug=False)