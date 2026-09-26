"""Final targeted probes. SELECT only."""
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

    hdr("1. thought_tokens NULL RATE (data quality, by model)")
    table(q(c, """
        SELECT model,
               count(*) AS calls_with_tokens,
               count(*) FILTER (WHERE thought_tokens IS NULL) AS thoughts_null,
               count(*) FILTER (WHERE thought_tokens = 0) AS thoughts_zero,
               count(*) FILTER (WHERE thought_tokens > 0) AS thoughts_pos
        FROM llm_call_log WHERE total_tokens IS NOT NULL
        GROUP BY 1 ORDER BY calls_with_tokens DESC
    """))

    hdr("2. THE ONE ROW THAT MATCHES NEITHER TOKEN IDENTITY")
    table(q(c, """
        SELECT to_char(ts, 'MM-DD HH24:MI:SS') AS ts, scope, model, attempt, status,
               prompt_tokens, output_tokens, thought_tokens, total_tokens,
               (prompt_tokens + output_tokens) AS p_plus_o,
               (prompt_tokens + output_tokens + coalesce(thought_tokens,0)) AS p_plus_o_plus_t
        FROM llm_call_log
        WHERE total_tokens IS NOT NULL
          AND total_tokens IS DISTINCT FROM (prompt_tokens + output_tokens)
          AND total_tokens IS DISTINCT FROM (prompt_tokens + output_tokens + coalesce(thought_tokens,0))
    """))

    hdr("3. SCOPE PREFIX CENSUS (is 'sync:' ever emitted?)")
    table(q(c, """
        SELECT split_part(scope, ':', 1) AS prefix, count(*) AS n
        FROM llm_call_log GROUP BY 1 ORDER BY n DESC
    """))

    hdr("4. TAIL OF THE 429 BURST 02:26 -> 02:35, every row")
    table(q(c, """
        SELECT to_char(ts, 'HH24:MI:SS.MS') AS ts, scope, model, status,
               attempt, latency_ms
        FROM llm_call_log
        WHERE ts >= '2026-09-25 02:26' AND ts < '2026-09-25 02:36'
        ORDER BY ts
    """))

    hdr("5. 429 COUNT vs RETRY COUNT, per 10-minute bucket (who is short?)")
    table(q(c, """
        SELECT to_char(date_trunc('hour', ts) + interval '10 min' * floor(extract(minute from ts)/10), 'MM-DD HH24:') 
                 || lpad((floor(extract(minute from ts)/10)*10)::text, 2, '0') AS bucket,
               count(*) FILTER (WHERE attempt = 1 AND status <> 'ok') AS a1_429,
               count(*) FILTER (WHERE attempt = 2) AS a2_rows,
               count(*) FILTER (WHERE attempt = 2 AND status = 'ok') AS a2_ok
        FROM llm_call_log
        GROUP BY 1 HAVING count(*) FILTER (WHERE attempt = 1 AND status <> 'ok') > 0
        ORDER BY 1
    """))

    hdr("6. LATENCY vs BUDGET — spread of successful single-attempt calls")
    table(q(c, """
        SELECT model,
               count(*) AS ok_calls,
               round(percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms)) AS p95_ms,
               round(percentile_disc(0.99) WITHIN GROUP (ORDER BY latency_ms)) AS p99_ms,
               max(latency_ms) AS max_ms,
               count(*) FILTER (WHERE latency_ms > 45000) AS over_budget_45s,
               count(*) FILTER (WHERE latency_ms > 120000) AS over_timeout_120s
        FROM llm_call_log WHERE status = 'ok' GROUP BY 1 ORDER BY ok_calls DESC
    """))

    hdr("7. TOKEN SPEND PER SCOPE (the cost conversation)")
    table(q(c, """
        SELECT scope, model,
               count(*) AS calls,
               sum(prompt_tokens) AS prompt,
               sum(coalesce(output_tokens,0)) AS output,
               sum(coalesce(thought_tokens,0)) AS thoughts,
               sum(total_tokens) AS total,
               round(avg(total_tokens)) AS avg_total
        FROM llm_call_log WHERE total_tokens IS NOT NULL
        GROUP BY 1,2 ORDER BY total DESC
    """))

e.dispose()
