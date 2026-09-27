"""Read-only health report for llm_call_log on prod.

Run: uv run python _diag_telemetry_prod.py
Absolutely no writes: every statement below is a SELECT.
"""
import os
from collections import defaultdict

from sqlalchemy import create_engine, text


def _load_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _url():
    _load_env()
    url = os.environ.get("PROD_DATABASE_URL")
    if not url:
        raise SystemExit("PROD_DATABASE_URL not found in environment/.env")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[13:]
    elif url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[11:]
    return url


def q(c, sql, **params):
    return [dict(r._mapping) for r in c.execute(text(sql), params)]


def hdr(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def table(rows, cols=None, widths=None):
    if not rows:
        print("  (no rows)")
        return
    cols = cols or list(rows[0].keys())
    widths = widths or {}
    w = {c: widths.get(c, max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows))) for c in cols}
    print("  " + "  ".join(str(c).ljust(w[c]) for c in cols))
    print("  " + "  ".join("-" * w[c] for c in cols))
    for r in rows:
        print("  " + "  ".join(str(r.get(c, "")).ljust(w[c]) for c in cols))


def main():
    e = create_engine(_url(), pool_pre_ping=True)
    with e.connect() as c:
        c.execute(text("SET statement_timeout = '120s'"))

        # ---------------------------------------------------------------- 0
        hdr("0. IS IT STILL RUNNING?  (freshness of the table)")
        table(q(c, """
            SELECT now() AS db_now,
                   max(ts) AS newest_row,
                   (now() - max(ts))::text AS age_of_newest,
                   count(*) AS total_rows
            FROM llm_call_log
        """))

        hdr("0b. ROWS PER HOUR (UTC) — gaps mean the writer died")
        table(q(c, """
            SELECT to_char(date_trunc('hour', ts), 'YYYY-MM-DD HH24:00') AS hour_utc,
                   count(*) AS rows,
                   count(*) FILTER (WHERE status <> 'ok') AS not_ok
            FROM llm_call_log
            GROUP BY 1 ORDER BY 1
        """))

        # ---------------------------------------------------------------- 1
        hdr("1. PER-MODEL HEALTH  (whole window)")
        table(q(c, """
            SELECT model,
                   count(*) AS calls,
                   count(*) FILTER (WHERE status = 'ok') AS ok,
                   count(*) FILTER (WHERE status <> 'ok') AS err,
                   round(100.0 * count(*) FILTER (WHERE status <> 'ok') / count(*), 1) AS err_pct,
                   round(avg(latency_ms)) AS avg_ms,
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
                   percentile_disc(0.9) WITHIN GROUP (ORDER BY latency_ms) AS p90_ms,
                   max(latency_ms) AS max_ms,
                   sum(total_tokens) AS tot_tokens
            FROM llm_call_log
            WHERE latency_ms IS NOT NULL
            GROUP BY model ORDER BY calls DESC
        """))

        # ---------------------------------------------------------------- 2
        hdr("2. ERROR BREAKDOWN")
        table(q(c, """
            SELECT model, error_class, http_status, attempt,
                   count(*) AS n,
                   min(ts) AS first_seen, max(ts) AS last_seen
            FROM llm_call_log
            WHERE status <> 'ok'
            GROUP BY 1,2,3,4 ORDER BY n DESC
        """))

        hdr("2b. STATUS / ERROR_CLASS MATRIX")
        table(q(c, """
            SELECT status, coalesce(error_class, '-') AS error_class, count(*) AS n
            FROM llm_call_log GROUP BY 1,2 ORDER BY n DESC
        """))

        # ---------------------------------------------------------------- 3
        hdr("3. FAILOVER — did a retry ever rescue a call?")
        table(q(c, """
            SELECT attempt, model,
                   count(*) AS n,
                   count(*) FILTER (WHERE status = 'ok') AS ok,
                   count(*) FILTER (WHERE status <> 'ok') AS err
            FROM llm_call_log GROUP BY 1,2 ORDER BY attempt, n DESC
        """))

        hdr("3b. ATTEMPT>1 ROWS — what did they fall back FROM")
        table(q(c, """
            SELECT attempt, fell_back_from, model,
                   count(*) AS n,
                   count(*) FILTER (WHERE status = 'ok') AS ok,
                   round(avg(latency_ms)) AS avg_ms
            FROM llm_call_log
            WHERE attempt > 1
            GROUP BY 1,2,3 ORDER BY n DESC
        """))

        hdr("3c. TERMINAL FAILURES (an attempt-1 row with NO later attempt for it)")
        table(q(c, """
            SELECT model, error_class, http_status, count(*) AS n
            FROM llm_call_log
            WHERE status <> 'ok' AND attempt = 1
            GROUP BY 1,2,3 ORDER BY n DESC
        """))

        # ---------------------------------------------------------------- 4
        hdr("4. DATA-QUALITY — null / constant-rate per column")
        table(q(c, """
            SELECT count(*) AS rows,
                   round(100.0*count(*) FILTER (WHERE service_tier IS NULL)/count(*),1) AS tier_null_pct,
                   round(100.0*count(*) FILTER (WHERE location IS NULL)/count(*),1) AS loc_null_pct,
                   round(100.0*count(*) FILTER (WHERE correlation_id IS NULL)/count(*),1) AS corr_null_pct,
                   round(100.0*count(*) FILTER (WHERE ttft_ms IS NULL)/count(*),1) AS ttft_null_pct,
                   round(100.0*count(*) FILTER (WHERE prompt_tokens IS NULL)/count(*),1) AS ptok_null_pct,
                   round(100.0*count(*) FILTER (WHERE total_tokens IS NULL)/count(*),1) AS ttok_null_pct,
                   round(100.0*count(*) FILTER (WHERE latency_ms IS NULL)/count(*),1) AS lat_null_pct
            FROM llm_call_log
        """))

        table(q(c, """
            SELECT DISTINCT service_tier, location FROM llm_call_log
        """))

        hdr("4b. TOKEN ARITHMETIC BY PATH — which identity holds?")
        table(q(c, """
            SELECT split_part(scope, ':', 1) AS path,
                   count(*) AS n,
                   count(*) FILTER (WHERE total_tokens = prompt_tokens + output_tokens + thought_tokens) AS matches_with_thoughts,
                   count(*) FILTER (WHERE total_tokens = prompt_tokens + output_tokens) AS matches_no_thoughts,
                   count(*) FILTER (WHERE thought_tokens > 0) AS has_thoughts
            FROM llm_call_log
            WHERE total_tokens IS NOT NULL GROUP BY 1 ORDER BY n DESC
        """))

        # ---------------------------------------------------------------- 5
        hdr("5. SCOPE x MODEL")
        table(q(c, """
            SELECT scope, model, count(*) AS calls,
                   count(*) FILTER (WHERE status <> 'ok') AS err,
                   round(avg(latency_ms)) AS avg_ms,
                   max(latency_ms) AS max_ms
            FROM llm_call_log
            GROUP BY 1,2 ORDER BY calls DESC LIMIT 30
        """))

        # ---------------------------------------------------------------- 6
        hdr("6. 429s PER HOUR PER MODEL — is throttling getting worse?")
        table(q(c, """
            SELECT to_char(date_trunc('hour', ts), 'MM-DD HH24:00') AS hour_utc,
                   model,
                   count(*) AS calls,
                   count(*) FILTER (WHERE http_status = 429) AS n429,
                   round(100.0*count(*) FILTER (WHERE http_status = 429)/count(*),1) AS pct429
            FROM llm_call_log
            WHERE split_part(scope, ':', 1) = 'pipeline' OR http_status = 429
            GROUP BY 1,2 HAVING count(*) FILTER (WHERE http_status = 429) > 0
            ORDER BY 1,2
        """))

        # ---------------------------------------------------------------- 7
        hdr("7. LATENCY OUTLIERS — 10 slowest ok calls")
        table(q(c, """
            SELECT to_char(ts, 'MM-DD HH24:MI') AS ts, scope, model,
                   latency_ms, prompt_tokens, output_tokens, total_tokens
            FROM llm_call_log WHERE status = 'ok'
            ORDER BY latency_ms DESC NULLS LAST LIMIT 10
        """))

        hdr("7b. TOKEN OUTLIERS — 5 biggest prompts")
        table(q(c, """
            SELECT to_char(ts, 'MM-DD HH24:MI') AS ts, scope, model,
                   prompt_tokens, output_tokens, thought_tokens, total_tokens, latency_ms
            FROM llm_call_log
            ORDER BY prompt_tokens DESC NULLS LAST LIMIT 5
        """))

        # ---------------------------------------------------------------- 8
        hdr("8. HOURLY TOTALS — volume + latency trend")
        table(q(c, """
            SELECT to_char(date_trunc('hour', ts), 'MM-DD HH24:00') AS hour_utc,
                   count(*) AS calls,
                   count(*) FILTER (WHERE status <> 'ok') AS err,
                   round(avg(latency_ms)) AS avg_ms,
                   percentile_disc(0.9) WITHIN GROUP (ORDER BY latency_ms) AS p90_ms,
                   sum(total_tokens) AS tokens,
                   count(DISTINCT model) AS models
            FROM llm_call_log GROUP BY 1 ORDER BY 1
        """))

        # ---------------------------------------------------------------- 9
        hdr("9. SCOPE COVERAGE — which parts of the app are instrumented")
        table(q(c, """
            SELECT scope, count(*) AS calls, min(ts)::date AS first_day, max(ts)::date AS last_day
            FROM llm_call_log GROUP BY 1 ORDER BY calls DESC
        """))

    e.dispose()


if __name__ == "__main__":
    main()
