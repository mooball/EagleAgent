"""Tight follow-up probes. SELECT only."""
import os

from sqlalchemy import create_engine, text


def _load_env(path=".env"):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _url():
    _load_env()
    u = os.environ["PROD_DATABASE_URL"]
    if u.startswith("postgresql://"):
        u = "postgresql+psycopg://" + u[13:]
    elif u.startswith("postgres://"):
        u = "postgresql+psycopg://" + u[11:]
    return u


def q(c, sql, **p):
    return [dict(r._mapping) for r in c.execute(text(sql), p)]


def hdr(t):
    print("\n" + "=" * 90 + "\n" + t + "\n" + "=" * 90)


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

    hdr("B2. email_tracking recency (app heartbeat)")
    table(q(c, """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE created_at > now() - interval '3 hours') AS created_3h,
               to_char(max(created_at), 'MM-DD HH24:MI') AS newest_created,
               to_char(max(updated_at), 'MM-DD HH24:MI') AS newest_updated
        FROM email_tracking
    """))

    hdr("B3. rows in llm_call_log in last 6h, vs email_tracking in last 6h")
    table(q(c, """
        SELECT (SELECT count(*) FROM llm_call_log WHERE ts > now() - interval '6 hours') AS llm_rows_6h,
               (SELECT count(*) FROM email_tracking WHERE created_at > now() - interval '6 hours') AS emails_6h,
               (SELECT count(*) FROM email_tracking WHERE updated_at > now() - interval '6 hours') AS email_updates_6h
    """))

    hdr("C. TOKEN ANOMALIES — only the pipeline path can be wrong")
    table(q(c, """
        SELECT scope, model,
               count(*) AS calls,
               count(*) FILTER (WHERE thought_tokens > 0
                                  AND total_tokens = prompt_tokens+output_tokens+thought_tokens) AS ok_with_thoughts,
               count(*) FILTER (WHERE thought_tokens > 0
                                  AND total_tokens = prompt_tokens+output_tokens) AS suspicious,
               count(*) FILTER (WHERE thought_tokens = 0) AS no_thoughts
        FROM llm_call_log
        WHERE total_tokens IS NOT NULL AND split_part(scope, ':', 1) = 'pipeline'
        GROUP BY 1,2 ORDER BY calls DESC
    """))

    hdr("C2. the actual suspicious rows (pipeline, thoughts>0, total excludes them)")
    table(q(c, """
        SELECT to_char(ts, 'MM-DD HH24:MI:SS') AS ts, scope, model,
               prompt_tokens, output_tokens, thought_tokens, total_tokens
        FROM llm_call_log
        WHERE total_tokens IS NOT NULL
          AND split_part(scope, ':', 1) = 'pipeline'
          AND thought_tokens > 0
          AND total_tokens = prompt_tokens + output_tokens
        ORDER BY ts
    """))

    hdr("D. THINKING BY SCOPE / PATH")
    table(q(c, """
        SELECT split_part(scope, ':', 1) AS path,
               count(*) AS calls,
               count(*) FILTER (WHERE thought_tokens > 0) AS with_thoughts,
               sum(thought_tokens) AS thoughts,
               sum(output_tokens) AS output,
               sum(total_tokens) AS total,
               round(100.0*sum(thought_tokens)/nullif(sum(total_tokens),0),1) AS thought_pct_of_total
        FROM llm_call_log WHERE total_tokens IS NOT NULL
        GROUP BY 1 ORDER BY thoughts DESC
    """))

    hdr("E. ORPHAN 429s — attempt-1 error with no attempt-2 within 60s")
    table(q(c, """
        WITH a1 AS (
            SELECT id, ts FROM llm_call_log
            WHERE attempt = 1 AND status <> 'ok'
        ), a2 AS (SELECT ts FROM llm_call_log WHERE attempt = 2)
        SELECT count(*) AS total_attempt1_errors,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM a2 WHERE a2.ts BETWEEN a1.ts - interval '5 seconds'
                                                   AND a1.ts + interval '60 seconds')
               ) AS retried,
               count(*) FILTER (WHERE NOT EXISTS (
                   SELECT 1 FROM a2 WHERE a2.ts BETWEEN a1.ts - interval '5 seconds'
                                                   AND a1.ts + interval '60 seconds')
               ) AS orphans
        FROM a1
    """))

    hdr("E2. ORPHAN DETAIL")
    table(q(c, """
        WITH a1 AS (
            SELECT ts, model, scope FROM llm_call_log
            WHERE attempt = 1 AND status <> 'ok'
        ), a2 AS (SELECT ts FROM llm_call_log WHERE attempt = 2)
        SELECT to_char(a1.ts, 'MM-DD HH24:MI:SS') AS ts, a1.scope, a1.model,
               (SELECT count(*) FROM a2
                 WHERE a2.ts BETWEEN a1.ts - interval '5 seconds' AND a1.ts + interval '60 seconds') AS sisters
        FROM a1
        WHERE NOT EXISTS (
            SELECT 1 FROM a2 WHERE a2.ts BETWEEN a1.ts - interval '5 seconds' AND a1.ts + interval '60 seconds')
    """))

    hdr("E3. 429 BURST SHAPE — 01:30-02:30 (each minute)")
    table(q(c, """
        SELECT to_char(date_trunc('minute', ts), 'HH24:MI') AS min_utc,
               model, attempt, status, count(*) AS n
        FROM llm_call_log
        WHERE ts >= '2026-09-25 01:30' AND ts < '2026-09-25 02:30'
          AND split_part(scope,':',1) = 'pipeline'
        GROUP BY 1,2,3,4 ORDER BY 1,3,2
    """))

    hdr("F. FAILOVER ACCOUNTING (whole window)")
    table(q(c, """
        SELECT count(*) FILTER (WHERE attempt = 1 AND status <> 'ok') AS a1_errors,
               count(*) FILTER (WHERE attempt = 2) AS a2_rows,
               count(*) FILTER (WHERE attempt = 2 AND status = 'ok') AS a2_ok,
               count(*) FILTER (WHERE attempt = 2 AND status <> 'ok') AS a2_err,
               count(*) FILTER (WHERE attempt IS NULL) AS attempt_null,
               count(*) FILTER (WHERE fell_back_from IS NOT NULL) AS fell_back
        FROM llm_call_log
    """))

    hdr("G. LATENCY vs THRESHOLD — how many calls exceed 30s / 60s")
    table(q(c, """
        SELECT model,
               count(*) AS calls,
               count(*) FILTER (WHERE latency_ms > 30000) AS over_30s,
               count(*) FILTER (WHERE latency_ms > 60000) AS over_60s,
               max(latency_ms) AS max_ms
        FROM llm_call_log WHERE latency_ms IS NOT NULL GROUP BY 1 ORDER BY calls DESC
    """))

    hdr("H. QUOTE/EXTRACT SUCCESS BY MODEL — is the mixed-model output real?")
    table(q(c, """
        SELECT model, status,
               count(*) AS n,
               round(avg(latency_ms)) AS avg_ms,
               sum(total_tokens) AS tokens
        FROM llm_call_log
        WHERE scope = 'pipeline:QUOTE/extract'
        GROUP BY 1,2 ORDER BY 1,2
    """))

e.dispose()
