# sql-coach-forge

> Local-first SQL query coach — paste a query, get coached on correctness, performance, and style.

Built for engineers and analysts who want a fast sanity-check before merging a PR, after a slow `EXPLAIN`, or while reviewing someone else's query. No LLM, no API keys, no data leaving your machine.

![architecture](https://img.shields.io/badge/stack-Flask%20%2B%20SQLite%20%2B%20vanilla%20JS-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![private](https://img.shields.io/badge/runs-local--first-orange)

## What it does

You paste:

```sql
SELECT u.country, COUNT(DISTINCT o.id),
       (SELECT MAX(created_at) FROM orders o2 WHERE o2.user_id = u.id)
FROM users u, orders o
WHERE u.signup_date BETWEEN '2026-01-01' AND '2026-01-31'
  AND u.email LIKE '%@gmail.com'
  AND YEAR(u.created_at) = 2026
  AND o.user_id = u.id
GROUP BY u.country
ORDER BY order_count DESC;
```

…with context like *"Snowflake on 50M row events table, prod warehouse, daily batch."*

You get back:

- **0–100 query-readiness score** across 5 axes (correctness / performance / readability / structure / portability)
- **30+ anti-pattern detections** (SELECT *, NOT IN with NULL, BETWEEN on datetime, leading wildcard LIKE, function-on-column, JOIN without ON, NULL = NULL, etc.) — each with severity (high/medium/low), evidence snippet, and a one-line fix
- **Pseudo execution plan** (Seq Scan / Index Seek / Hash Join / Hash Aggregate / Top-N Sort / Full Sort, etc.) with cost estimates
- **Suggested indexes** with rationale (btree, cluster key, FTS, trigram)
- **Refactor library** with before/after diffs (e.g. `IN → EXISTS`, `NOT IN → NOT EXISTS`, `UNION → UNION ALL`, `BETWEEN datetime → half-open range`, `scalar subquery to JOIN + GROUP BY`, `= NULL → IS NULL`, divide-by-zero via NULLIF)
- **Flavor-aware hints** for 11 dialects (Snowflake, BigQuery, Postgres, SQL Server, MySQL, Oracle, DuckDB, Databricks, Spark, Redshift, generic)
- **SQLite session log** with self-rating (1–5) + notes + Markdown / JSON export

## Why it matters

Most "SQL is slow" or "SQL has a bug" moments are patterns, not mysteries. The same 12–15 anti-patterns account for the majority of production SQL issues — `NOT IN` returning empty results because of a NULL, `LIKE '%foo'` killing an index, `YEAR(col) = 2026` blocking index use, `BETWEEN` on datetime silently dropping the last second, scalar subqueries hiding N+1, JOINs without ON, and so on.

This tool surfaces them in 200ms without sending your SQL anywhere.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Backend | Flask + SQLite | Single-file server, zero-config DB, no external services |
| Frontend | Vanilla JS + CSS, dark theme | No build step, no framework overhead, loads in <50ms |
| Analysis | Pure regex + lightweight token logic | Deterministic, debuggable, no LLM |
| Persistence | SQLite (one file: `sql_coach.sqlite3`) | Local-first, easy to back up, easy to delete |

## Run it locally

```bash
# 1. install deps
pip install -r requirements.txt

# 2. start the server
python app.py
# → http://127.0.0.1:5001

# 3. run the test suite
python test_smoke.py
```

That's it. No environment variables, no API keys, no database server.

## REST API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness check |
| `POST` | `/api/analyze` | Analyze a query — body: `{query, context}` |
| `GET` | `/api/sessions` | List saved sessions (most recent 100) |
| `POST` | `/api/sessions` | Save a session — body: `{query, context, result}` |
| `GET` | `/api/sessions/<id>` | Fetch one session with full result |
| `PATCH` | `/api/sessions/<id>` | Update self-rating and/or notes |
| `GET` | `/api/export/<id>?fmt=md\|json` | Export a session as Markdown or JSON |

## Anti-pattern library

30+ rules covering:

- **Correctness**: `NULL = NULL`, `<> NULL`, `NOT IN` with NULL, `JOIN` missing ON, missing WHERE on UPDATE/DELETE, between-on-datetime off-by-one, division by zero, redundant DISTINCT papering over a bad JOIN, CASE without ELSE
- **Performance**: `SELECT *`, function-on-column in WHERE, leading wildcard LIKE / ILIKE, OR across indexed columns, OR between different columns, scalar subquery in SELECT, unbounded ORDER BY, UNION vs UNION ALL, IN with large subquery, very long IN list, CAST in WHERE
- **Readability**: comma joins, DISTINCT + GROUP BY, GROUP BY ordinal position, no FROM, missing alias on JOIN, deep subquery nesting (≥ 3)
- **Structure**: same table joined twice without alias

## Flavor hints

The tool detects 11 SQL dialects from the query text + context:
Snowflake, BigQuery, Postgres, SQL Server, MySQL, Oracle, DuckDB, Databricks, Spark, Redshift, plus a generic fallback. Each ships with 2–4 targeted hints (e.g. *BigQuery: APPROX_COUNT_DISTINCT for billion-row counts*, *Postgres: pg_trgm GIN for ILIKE*, *SQL Server: OPTION(RECOMPILE) for parameter sniffing*).

## Context-aware analysis

The optional context field ("50M rows, daily batch, prod warehouse") drives:

- **Row count pressure** — multi-million-row scans get a warning in the plan
- **Severity adjustment** — `ORDER BY` without LIMIT is *low* in an ad-hoc investigation but *high* in a scheduled prod pipeline
- **Flavor hint bias** — flavor is auto-detected, but explicit mentions win
- **Weight shift** — ad-hoc queries weight readability + portability more, prod queries weight correctness + performance more

## Privacy

- The Flask server binds to `127.0.0.1` only — never exposed off the loopback interface
- SQLite file lives next to `app.py` — back it up or delete it; nothing else stores your data
- No outbound HTTP — the analyzer is fully deterministic

## Project structure

```
sql-coach-forge/
├── app.py                # Flask app + REST API + analyzer engine
├── static/
│   ├── index.html        # Single-page UI
│   ├── style.css         # Dark theme
│   └── app.js            # Vanilla-JS controller
├── test_smoke.py         # 30+ tests across all anti-patterns
├── requirements.txt      # Flask
├── README.md             # You are here
└── sql_coach.sqlite3     # auto-created on first run
```

## Tested scenarios

The smoke suite (`test_smoke.py`) covers:

- Flavor detection for all 11 dialects
- Context parsing (row counts in M/B/K notation, prod flag, warehouse flag, concerns)
- Each of the 30+ anti-patterns fires on a known-bad sample
- A "solid" query (CTE + EXISTS + half-open range + LIMIT) scores ≥ 80
- A "classic bad" query (sample above) scores < 60 with high-severity findings on each major trap
- Refactors trigger for the matching findings
- Index suggestions cover equality and ORDER BY columns
- Ad-hoc context lowers severity vs prod context

## License

MIT — do whatever you want, just don't blame me if it misses an anti-pattern your team uses.