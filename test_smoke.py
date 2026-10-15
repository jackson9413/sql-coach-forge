"""Smoke tests for sql-coach-forge — run with `python test_smoke.py`.

No pytest dependency — keeps the test runner portable.
"""
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

# Use a temp DB so the test never collides with a real one
os.environ["SQL_COACH_DB"] = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False).name

import app as app_module  # noqa: E402
from app import analyze, detect_flavor, parse_context, normalize  # noqa: E402

app_module.init_db()


class TestFlavor(unittest.TestCase):
    def test_snowflake_qualify(self):
        self.assertEqual(detect_flavor("SELECT * FROM t QUALIFY row_num = 1", ""), "snowflake")

    def test_bigquery_approx(self):
        self.assertEqual(detect_flavor("SELECT APPROX_COUNT_DISTINCT(id) FROM t", ""), "bigquery")

    def test_postgres_date_trunc(self):
        self.assertEqual(detect_flavor("SELECT date_trunc('day', ts) FROM t", ""), "postgres")

    def test_sqlserver_datediff(self):
        self.assertEqual(detect_flavor("SELECT DATEDIFF(d, a, b) FROM t", ""), "sqlserver")

    def test_mysql_group_concat(self):
        self.assertEqual(detect_flavor("SELECT GROUP_CONCAT(name) FROM t", ""), "mysql")

    def test_oracle_nvl(self):
        self.assertEqual(detect_flavor("SELECT NVL(col, 0) FROM t", ""), "oracle")

    def test_databricks_qualify(self):
        self.assertEqual(detect_flavor("SELECT date_diff(a, b) FROM t", ""), "databricks")

    def test_generic_when_no_signal(self):
        self.assertEqual(detect_flavor("SELECT id FROM t", ""), "generic")


class TestContext(unittest.TestCase):
    def test_row_count_parsing(self):
        c = parse_context("Snowflake on 50M row events table")
        self.assertEqual(c["row_count"], 50_000_000)

    def test_row_count_billion(self):
        c = parse_context("1.2B rows, bigquery")
        self.assertEqual(c["row_count"], 1_200_000_000)

    def test_is_prod(self):
        c = parse_context("production pipeline")
        self.assertTrue(c["is_prod"])

    def test_is_warehouse(self):
        c = parse_context("snowflake warehouse")
        self.assertTrue(c["is_warehouse"])

    def test_concerns(self):
        c = parse_context("slow, expensive, duplicate")
        self.assertIn("performance", c["concerns"])
        self.assertIn("cost", c["concerns"])
        self.assertIn("correctness", c["concerns"])


class TestAnalyze(unittest.TestCase):
    def assertHasFinding(self, result, label, severity=None):
        for f in result["findings"]:
            if f["label"] == label:
                if severity is None or f["severity"] == severity:
                    return f
        self.fail(f"Expected finding {label!r} (severity={severity}) in {[f['label'] for f in result['findings']]}")

    def assertScoreIn(self, result, axis, lo, hi):
        v = result["scores"][axis]
        self.assertGreaterEqual(v, lo, f"{axis}={v} < {lo}")
        self.assertLessEqual(v, hi, f"{axis}={v} > {hi}")

    def test_classic_bad_query_catches_everything(self):
        # The sample we ship in the UI — should light up many findings
        q = """
        SELECT u.country, COUNT(DISTINCT o.id),
               (SELECT MAX(created_at) FROM orders o2 WHERE o2.user_id = u.id)
        FROM users u, orders o
        WHERE u.signup_date BETWEEN '2026-01-01' AND '2026-01-31'
          AND u.email LIKE '%@gmail.com'
          AND YEAR(u.created_at) = 2026
          AND o.user_id = u.id
        GROUP BY u.country
        ORDER BY order_count DESC;
        """
        r = analyze(q, "snowflake 50M row events table prod warehouse")
        # Note: the classic-bad query doesn't actually have `SELECT *` — it lists columns.
        # We DO expect these to fire:
        self.assertHasFinding(r, "Old-style comma JOINs")
        self.assertHasFinding(r, "LIKE with leading wildcard")
        self.assertHasFinding(r, "Function wrapping indexed column")
        self.assertHasFinding(r, "BETWEEN on datetime (off-by-one risk)")
        self.assertHasFinding(r, "ORDER BY without LIMIT")
        self.assertHasFinding(r, "Correlated scalar subquery in SELECT")
        self.assertIn(r["complexity"], ("large", "expensive"))
        self.assertLess(r["scores"]["overall"], 60)

    def test_solid_query_scores_high(self):
        q = """
        WITH active_users AS (
            SELECT id, country
            FROM users
            WHERE created_at >= '2026-01-01'
              AND created_at < '2026-02-01'
              AND deleted_at IS NULL
        )
        SELECT a.country, COUNT(*) AS user_count
        FROM active_users a
        WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = a.id)
        GROUP BY a.country
        ORDER BY user_count DESC
        LIMIT 100;
        """
        r = analyze(q, "snowflake ad-hoc analysis")
        self.assertEqual(r["complexity"], "small")
        self.assertGreaterEqual(r["scores"]["overall"], 80)
        self.assertIn(r["verdict"], ("mostly_ready", "ship_ready"))

    def test_null_eq_is_high_correctness(self):
        q = "SELECT * FROM users WHERE deleted_at = NULL"
        r = analyze(q, "")
        self.assertHasFinding(r, "NULL = NULL (always false)", "high")
        self.assertLess(r["scores"]["correctness"], 90)

    def test_not_in_fires_high(self):
        q = "SELECT * FROM users WHERE id NOT IN (SELECT user_id FROM banned)"
        r = analyze(q, "")
        self.assertHasFinding(r, "NOT IN with potentially-NULL list", "high")

    def test_join_no_on_is_high(self):
        q = "SELECT * FROM a JOIN b"
        r = analyze(q, "")
        self.assertHasFinding(r, "JOIN missing ON/USING", "high")

    def test_leading_wildcard_like(self):
        q = "SELECT id FROM users WHERE email LIKE '%@gmail.com'"
        r = analyze(q, "")
        self.assertHasFinding(r, "LIKE with leading wildcard", "medium")

    def test_function_on_column(self):
        q = "SELECT id FROM users WHERE YEAR(created_at) = 2026"
        r = analyze(q, "")
        self.assertHasFinding(r, "Function wrapping indexed column")

    def test_union_should_be_union_all(self):
        q = "SELECT id FROM a UNION SELECT id FROM b"
        r = analyze(q, "")
        self.assertHasFinding(r, "UNION instead of UNION ALL", "low")

    def test_refactors_triggered_for_findings(self):
        q = "SELECT * FROM a WHERE id NOT IN (SELECT id FROM b)"
        r = analyze(q, "")
        refactor_ids = {x["id"] for x in r["refactors"]}
        self.assertIn("not_exists_over_not_in", refactor_ids)
        self.assertIn("exists_over_in", refactor_ids) or self.assertIn("not_exists_over_not_in", refactor_ids)

    def test_index_suggestions(self):
        q = "SELECT * FROM orders WHERE user_id = 42 ORDER BY created_at DESC"
        r = analyze(q, "")
        cols = {i["column"] for i in r["indexes"]}
        self.assertIn("user_id", cols)
        self.assertIn("created_at", cols)

    def test_flavor_hints_present(self):
        q = "SELECT id FROM t"
        r = analyze(q, "snowflake on 50M rows prod")
        self.assertGreater(len(r["flavor_hints"]), 0)
        # Should be snowflake hints (since flavor detection chose snowflake)
        joined = " ".join(r["flavor_hints"]).lower()
        self.assertTrue(any(k in joined for k in ["cluster", "qualify", "snowflake"]))

    def test_complexity_tiers(self):
        small = analyze("SELECT 1", "")
        self.assertEqual(small["complexity"], "small")
        big = analyze("SELECT a.x FROM t1 a JOIN t2 b JOIN t3 c JOIN t4 d JOIN t5 e ON 1=1", "1B rows prod")
        self.assertIn(big["complexity"], ("large", "expensive"))

    def test_pseudo_plan_present(self):
        r = analyze("SELECT id FROM users WHERE email = 'x'", "")
        self.assertGreater(len(r["plan"]), 0)
        ops = [p["op"] for p in r["plan"]]
        self.assertTrue(any("Scan" in o or "Seek" in o for o in ops))

    def test_adhoc_lowers_severity(self):
        q = "SELECT * FROM users ORDER BY id"
        adhoc = analyze(q, "ad-hoc investigation")
        prod = analyze(q, "prod critical warehouse, daily batch")
        adhoc_sev = next((f["severity"] for f in adhoc["findings"] if f["label"] == "ORDER BY without LIMIT"), None)
        prod_sev = next((f["severity"] for f in prod["findings"] if f["label"] == "ORDER BY without LIMIT"), None)
        self.assertEqual(adhoc_sev, "low")
        self.assertEqual(prod_sev, "high")


class TestNormalize(unittest.TestCase):
    def test_strips_line_comments(self):
        self.assertNotIn("--", normalize("SELECT 1 -- this is a comment"))

    def test_strips_block_comments(self):
        self.assertNotIn("hi", normalize("SELECT /* hi */ 1"))

    def test_collapses_whitespace(self):
        self.assertNotIn("\n", normalize("SELECT\n\n   1\nFROM\n   t"))
        self.assertIn("1", normalize("SELECT\n\n   1\nFROM\n   t"))


if __name__ == "__main__":
    unittest.main(verbosity=2)