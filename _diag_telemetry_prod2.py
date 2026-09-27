"""Follow-up read-only probes for llm_call_log on prod. SELECT only."""
import os

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
    url = os.environ["PROD_DATABASE_URL"]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[13:]
    elif url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[11:]
    return url


def q(c, sql, **p):
    return [dict(r._mapping) for r in c.execute(text(sql), p)]


def hdr(t):
    print("\n" + "=" * 78 + "\n" + t + "\n" + "=" * 78)


def table(rows, cols=None):
    if not rows:
        print("  (no rows)")
        return
    cols = cols or list(rows[0].keys())
    w = {c: max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  " + "  ".join(str(c).ljust(w[c]) for c in cols))
    print("  " + "  ".join("-" * w[c] for c in cols))
    for r in rows:
        print("  " + "  ".join(str(r.get(c, "")).ljust(w[c]) for c in cols))


e = create_engine(_url(), pool_pre_ping=True)
with e.connect() as c:
    c.execute(text("SET statement_timeout = '120s'"))

    hdr("A. SILENCE GAPS > 20min in llm_call_log")
    table(q(c, """
        SELECT to_char(ts, 'MM-DD HH24:MI') AS gap_ends_at,
               (ts - prev_ts)::text AS gap
        FROM (SELECT ts, lag(ts) OVER (ORDER BY ts) AS prev_ts FROM llm_call_log) s
        WHERE prev_ts IS NOT NULL AND ts - prev_ts > interval '20 minutes'
        ORDER BY ts DESC
    """))

    hdr("A2. LAST 15 ROWS (is the writer alive?)")
    table(q(c, """
        SELECT to_char(ts, 'MM-DD HH24:MI:SS') AS ts, scope, model, status,
               latency_ms, total_tokens
        FROM llm_call_log ORDER BY ts DESC LIMIT 15
    """))

    hdr("B. IS THE APP STILL DOING ANYTHING? — table row counts + recency")
    table(q(c, """
        SELECT relname AS tbl,
               n_live_tup AS est_rows,
               to_char(greatest(last_vacuum, last_autovacuum, last_analyze, last_autoanalyze),
                       'MM-DD HH24:MI') AS last_maint
        FROM pg_stat_user_tables
        WHERE n_live_tup > 0
        ORDER BY n_live_tup DESC LIMIT 15
    """))

    hdr("B2. email_tracking recency (created_at)")
    table(q(c, """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE created_at > now() - interval '3 hours') AS last_3h,
               count(*) FILTER (WHERE created_at > now() - interval '1 day') AS last_24h,
               to_char(max(created_at), 'MM-DD HH24:MI') AS newest_created,
               to_char(max(updated_at), 'MM-DD HH24:MI') AS newest_updated
        FROM email_tracking
    """))

    hdr("C. THE TOKEN ANOMALY — rows where the two identities disagree")
    table(q(c, """
        SELECT to_char(ts, 'MM-DD HH24:MI:SS') AS ts, scope, model, status,
               attempt, prompt_tokens, output_tokens, thought_tokens, total_tokens,
               (total_tokens - prompt_tokens - output_tokens - thought_tokens) AS with_th_delta,
               (total_tokens - prompt_tokens - output_tokens) AS no_th_delta
        FROM llm_call_log
        WHERE total_tokens IS NOT NULL
          AND total_tokens <> prompt_tokens + output_tokens + thought_tokens
        ORDER BY ts
    """))

    hdr("D. THOUGHT-TOKEN RATIO BY SCOPE (where is thinking actually used?)")
    table(q(c, """
        SELECT scope,
               count(*) AS calls,
               count(*) FILTER (WHERE thought_tokens > 0) AS with_thoughts,
               sum(thought_tokens) AS thoughts,
               sum(output_tokens) AS output,
               round(100.0*sum(thought_tokens)/nullif(sum(total_tokens),0),1) AS thought_pct_of_total
        FROM llm_call_log WHERE total_tokens IS NOT NULL
        GROUP BY 1 ORDER BY thoughts DESC NULLS LAST
    """))

    hdr("E. THE ORPHAN 429 — attempt-1 error with no attempt-2 sibling")
    table(q(c, """
        WITH a1 AS (
            SELECT ts FROM llm_call_log
            WHERE attempt = 1 AND status <> 'ok' AND model = 'gemini-3.8-flash'
        ), a2 AS (
            SELECT ts FROM llm_call_log WHERE attempt = 2
        )
        SELECT a1.ts AS orphan_ts,
               (SELECT count(*) FROM a2
                 WHERE a2.ts BETWEEN a1.ts AND a1.ts + interval '30 seconds') AS sisters_30s,
               (SELECT count(*) FROM a2
                 WHERE a2.ts BETWEEN a1.ts - interval '30 seconds' AND a1.ts + interval '30 seconds') AS sisters_pm30s
        FROM a1
        WHERE NOT EXISTS (
            SELECT 1 FROM a2 WHERE a2.ts BETWEEN a1.ts AND a1.ts + interval '30 seconds'
        )
        ORDER BY a1.ts
    """))

    hdr("E2. 429s BURST SHAPE — the 02:00 hour")
    table(q(c, """
        SELECT to_char(ts, 'HH24:MI') AS minute_utc, status, attempt, count(*) AS n
        FROM llm_call_log
        WHERE ts >= '2026-09-25 02:00' AND ts < '2026-09-25 02:30'
        GROUP BY 1,2,3 ORDER BY 1,2,3
    """))

    hdr("F. ATTEMPT-2 ROWS vs ATTEMPT-1 CLAIMS — pairing count")
    table(q(c, """
        SELECT
          count(*) FILTER (WHERE attempt = 1 AND status <> 'ok') AS attempt1_errors,
          count(*) FILTER (WHERE attempt = 2) AS attempt2_rows,
          count(*) FILTER (WHERE attempt = 2 AND status = 'ok') AS attempt2_ok,
          count(*) FILTER (WHERE attempt = 2 AND status <> 'ok') AS attempt2_err,
          count(*) FILTER (WHERE attempt IS NULL) AS attempt_null
        FROM llm_call_log
    """))

    hdr("G. ACTIVE MODELS ON PROD (raw recent calls, error classes)")
    table(q(c, """
        SELECT to_char(ts, 'MM-DD HH24:MI') AS ts, scope, model, status, error_class,
               http_status, attempt, latency_ms
        FROM llm_call_log
        WHERE status <> 'ok' AND ts > now() - interval '8 hours'
        ORDER BY ts DESC LIMIT 12
    """))

e.dispose()
