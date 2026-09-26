"""Verify the telemetry insert path, and the row shape `_normalise` produces.

Adds rows inside a transaction that is **always rolled back** — nothing is
committed, so it is safe to re-run against the dev database.

Answers two questions:

1. Do the rows `telemetry._normalise` produces insert cleanly into the real
   `llm_call_log` table?
2. Was the original worry true — does a batch mixing a full row with a sparse one
   fail the write? (It does not. SQLAlchemy tolerates a heterogeneous
   executemany, which is why the all-columns change in `_normalise` is about
   consistency rather than avoiding a crash.)

Local database only. Never point this at PROD_DATABASE_URL.
"""
from sqlalchemy import create_engine, insert, text

from includes.dashboard.models import LlmCallLog
from includes.llm import telemetry

LOCAL = "postgresql+psycopg://postgres:postgres@localhost:5432/eagleagent"
MARKER = "test:%"

engine = create_engine(LOCAL, pool_pre_ping=True)


def _probe(label: str, rows: list[dict]) -> bool:
    """Insert rows, report the outcome, then always roll back."""
    conn = engine.connect()
    trans = conn.begin()
    ok = True
    try:
        conn.execute(insert(LlmCallLog), rows)
        visible = conn.execute(
            text("SELECT count(*) FROM llm_call_log WHERE scope LIKE :m"),
            {"m": MARKER},
        ).scalar()
        print(f"  {label}: insert OK ({visible} test rows visible in-transaction)")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  {label}: insert FAILED — {type(exc).__name__}: {str(exc)[:150]}")
    finally:
        trans.rollback()
        conn.close()
    return ok


with engine.connect() as c:
    present = c.execute(text("SELECT to_regclass('public.llm_call_log')")).scalar()
print(f"llm_call_log present locally: {bool(present)}")
if not present:
    raise SystemExit("no llm_call_log locally — run `uv run alembic upgrade head`")

print(f"_COLUMNS has {len(telemetry._COLUMNS)} columns")

# --- the two rows a single batch can realistically mix -----------------------
sparse = telemetry._normalise({"scope": "test:sparse", "model": "m"})
full = telemetry._normalise(
    {
        "scope": "test:full",
        "model": "gemini-x",
        "latency_ms": 123,
        "prompt_tokens": 100,
        "output_tokens": 30,
        "thought_tokens": 20,
        "total_tokens": 150,
    }
)
print(f"sparse keys={len(sparse)}  full keys={len(full)}  same shape: {set(sparse) == set(full)}")

# The invariant must hold on a real row too, not just in unit tests.
assert (
    full["prompt_tokens"] + full["output_tokens"] + full["thought_tokens"]
    == full["total_tokens"]
), "token invariant violated"

print("\n1. current row shape")
_probe("full + sparse (new)", [sparse, full])

print("\n2. pre-A2 row shape (only the keys the caller passed)")
old_sparse = {
    k: v for k, v in sparse.items()
    if k in ("ts", "scope", "provider", "model", "status")
}
_probe("full + sparse (old)", [old_sparse, dict(full)])

with engine.connect() as c:
    left = c.execute(
        text("SELECT count(*) FROM llm_call_log WHERE scope LIKE :m"), {"m": MARKER}
    ).scalar()
print(f"\ntest rows remaining after rollback: {left}")
assert left == 0, f"left {left} rows behind — this script must not persist anything"

engine.dispose()
print("\nOK")
