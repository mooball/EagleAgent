"""Read-only review of whether A4 still matters after the ladder change.

Run: uv run python _diag_a4_review.py
SELECTs only.
"""
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
    print("\n" + "=" * 92 + "\n" + t + "\n" + "=" * 92)


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

    hdr("0. WINDOW NOW AVAILABLE")
    table(q(c, """
        SELECT count(*) AS rows, min(ts) AS oldest, max(ts) AS newest,
               (now() - max(ts))::text AS age
        FROM llm_call_log
    """))

    hdr("1. PER-MODEL TOTALS (all data)")
    table(q(c, """
        SELECT model, count(*) AS calls,
               count(*) FILTER (WHERE http_status = 429) AS n429,
               round(100.0*count(*) FILTER (WHERE http_status = 429)/count(*),1) AS pct429,
               count(DISTINCT date_trunc('hour', ts)) AS active_hours,
               round(count(*)::numeric / greatest(count(DISTINCT date_trunc('hour', ts)),1), 1) AS calls_per_active_hour
        FROM llm_call_log GROUP BY 1 ORDER BY calls DESC
    """))

    hdr("2. REQUEST RATE + THROTTLING PER MODEL PER HOUR (any hour with a 429, or 3.8-flash)")
    table(q(c, """
        SELECT to_char(date_trunc('hour', ts), 'MM-DD HH24:00') AS hour_utc,
               model, count(*) AS calls,
               count(*) FILTER (WHERE http_status = 429) AS n429,
               round(100.0*count(*) FILTER (WHERE http_status = 429)/count(*),1) AS pct429,
               round(avg(latency_ms)) AS avg_ms
        FROM llm_call_log
        GROUP BY 1,2
        HAVING count(*) FILTER (WHERE http_status = 429) > 0 OR model = 'gemini-3.8-flash'
        ORDER BY 1, 2
    """))

    hdr("3. THE LOAD-CONCENTRATION RISK: what rate does each model actually see?")
    table(q(c, """
        SELECT model,
               count(*) AS calls,
               round(count(*)::numeric / 24.0, 1) AS calls_per_clock_hour,
               max(h) AS busiest_hour_calls,
               (SELECT count(*) FROM llm_call_log x WHERE x.model = l.model
                  AND x.http_status = 429) AS total_429
        FROM llm_call_log l
        JOIN LATERAL (
          SELECT count(*) AS h FROM llm_call_log y
          WHERE y.model = l.model GROUP BY date_trunc('hour', y.ts)
          ORDER BY count(*) DESC LIMIT 1
        ) busiest ON true
        GROUP BY model ORDER BY calls DESC
    """))

    hdr("4. QUOTE/extract — which model ACTUALLY served each logical call?")
    table(q(c, """
        SELECT model, status, attempt, count(*) AS n,
               round(avg(latency_ms)) AS avg_ms,
               round(avg(total_tokens)) AS avg_tokens
        FROM llm_call_log
        WHERE scope = 'pipeline:QUOTE/extract'
        GROUP BY 1,2,3 ORDER BY 1,2,3
    """))

    hdr("5. UNDER THE NEW LADDER: how many 3.8-flash 429s would retry onto 3.6-flash?")
    table(q(c, """
        SELECT count(*) FILTER (WHERE model = 'gemini-3.8-flash' AND http_status = 429)
                 AS would_go_to_3_6,
               count(*) FILTER (WHERE model = 'gemini-3.8-flash' AND http_status = 429
                                  AND scope LIKE 'pipeline:%') AS pipeline_only,
               count(*) FILTER (WHERE model = 'gemini-3.1-pro-preview' AND http_status = 429)
                 AS pro_429s
        FROM llm_call_log
    """))

    hdr("6. IS 3.6-flash SHOWING ANY STRAIN? (429s / latency over the window)")
    table(q(c, """
        SELECT to_char(date_trunc('hour', ts), 'MM-DD HH24:00') AS hour_utc,
               scope, count(*) AS calls, count(*) FILTER (WHERE http_status=429) AS n429,
               round(avg(latency_ms)) AS avg_ms, max(latency_ms) AS max_ms
        FROM llm_call_log WHERE model = 'gemini-3.6-flash'
        GROUP BY 1,2 ORDER BY 1,2
    """))

    hdr("7. WEEKEND vs WORKING DAY (has the pattern changed since 09-25 08:10?)")
    table(q(c, """
        SELECT to_char(date_trunc('day', ts), 'YYYY-MM-DD') AS day, model,
               count(*) AS calls, count(*) FILTER (WHERE http_status=429) AS n429
        FROM llm_call_log GROUP BY 1,2 ORDER BY 1,2
    """))

e.dispose()
