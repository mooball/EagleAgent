"""Chronological failover pairing. SELECT only."""
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


def q(c, sql):
    return [dict(r._mapping) for r in c.execute(text(sql))]


def hdr(t):
    print("\n" + "=" * 84 + "\n" + t + "\n" + "=" * 84)


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

    hdr("1. NEXT attempt-2 ROW AFTER EACH attempt-1 429 (same scope)")
    table(q(c, """
        WITH errs AS (
            SELECT ts, scope FROM llm_call_log
            WHERE attempt = 1 AND status <> 'ok'
        ), retries AS (
            SELECT ts, scope, status FROM llm_call_log WHERE attempt = 2
        )
        SELECT count(*) AS errors,
               count(*) FILTER (WHERE nxt_ts IS NULL) AS never_retried,
               count(*) FILTER (WHERE nxt_ts IS NOT NULL) AS retried,
               round(max(gap_s)::numeric, 1) AS max_gap_s,
               round(avg(gap_s)::numeric, 1) AS avg_gap_s
        FROM (
            SELECT e.scope,
                   (SELECT r.ts FROM retries r
                     WHERE r.scope = e.scope AND r.ts >= e.ts
                     ORDER BY r.ts LIMIT 1) AS nxt_ts,
                   (SELECT extract(epoch FROM (r.ts - e.ts)) FROM retries r
                     WHERE r.scope = e.scope AND r.ts >= e.ts
                     ORDER BY r.ts LIMIT 1) AS gap_s
            FROM errs e
        ) s
    """))

    hdr("2. THE UNRETRIED 429 (if any)")
    table(q(c, """
        WITH errs AS (
            SELECT id, ts, scope, model, latency_ms FROM llm_call_log
            WHERE attempt = 1 AND status <> 'ok'
        ), retries AS (
            SELECT ts, scope FROM llm_call_log WHERE attempt = 2
        )
        SELECT to_char(e.ts, 'MM-DD HH24:MI:SS') AS ts, e.scope, e.latency_ms,
               (SELECT min(r.ts) FROM retries r WHERE r.scope = e.scope AND r.ts >= e.ts) AS next_retry
        FROM errs e
        WHERE NOT EXISTS (SELECT 1 FROM retries r WHERE r.scope = e.scope AND r.ts >= e.ts)
        ORDER BY e.ts
    """))

    hdr("3. RETRY GAP DISTRIBUTION (how long a 429 costs us)")
    table(q(c, """
        WITH errs AS (SELECT ts, scope FROM llm_call_log WHERE attempt = 1 AND status <> 'ok'),
             retries AS (SELECT ts, scope FROM llm_call_log WHERE attempt = 2)
        SELECT bucket, count(*) AS n FROM (
            SELECT CASE
                     WHEN gap <= 2 THEN '<=2s'
                     WHEN gap <= 5 THEN '2-5s'
                     WHEN gap <= 15 THEN '5-15s'
                     WHEN gap <= 30 THEN '15-30s'
                     ELSE '>30s' END AS bucket
            FROM (
                SELECT (SELECT extract(epoch FROM (r.ts - e.ts)) FROM retries r
                         WHERE r.scope = e.scope AND r.ts >= e.ts
                         ORDER BY r.ts LIMIT 1) AS gap
                FROM errs e
            ) g WHERE gap IS NOT NULL
        ) b GROUP BY 1 ORDER BY min(
            CASE bucket WHEN '<=2s' THEN 1 WHEN '2-5s' THEN 2 WHEN '5-15s' THEN 3
                        WHEN '15-30s' THEN 4 ELSE 5 END)
    """))

    hdr("4. THE ONE TERMINAL FAILURE — context around 04:13")
    table(q(c, """
        SELECT to_char(ts, 'MM-DD HH24:MI:SS') AS ts, scope, model, status,
               attempt, fell_back_from, http_status, error_class, latency_ms
        FROM llm_call_log
        WHERE ts BETWEEN '2026-09-25 04:12:00' AND '2026-09-25 04:16:00'
        ORDER BY ts
    """))

    hdr("5. attempted chains that ended in error (status<>ok with attempt>=2)")
    table(q(c, """
        SELECT to_char(ts, 'MM-DD HH24:MI:SS') AS ts, scope, model, attempt,
               fell_back_from, http_status
        FROM llm_call_log WHERE status <> 'ok' AND attempt > 1
    """))

    hdr("6. HOW MANY DISTINCT PIPELINE RUNS?  (groups of calls within 30s)")
    table(q(c, """
        WITH t AS (
            SELECT ts, ts - (row_number() OVER (ORDER BY ts)) * interval '0' AS _x
            FROM llm_call_log
        )
        SELECT count(*) AS calls, min(ts)::date AS from_day, max(ts)::date AS to_day
        FROM t
    """))

e.dispose()
