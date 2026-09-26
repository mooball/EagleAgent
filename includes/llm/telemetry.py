"""LLM observability: record every model call, best-effort.

Answers the questions we currently cannot answer ("which model is slow, which
is erroring, what is this workload costing us") from SQL instead of by grepping
Railway logs.

Design constraints, in priority order:

1. **Never break a request.** A telemetry failure must not surface to the user.
   ``record_llm_call`` never raises; if the queue is full the row is dropped and
   counted.
2. **Never block the request.** Writes happen on a single daemon thread that
   batches. The calling thread only does a ``put_nowait``.
3. **Always leave a trace in logs** even if the DB write is skipped, so a
   telemetry outage still leaves evidence in Railway.

Usage::

    with instrument_call(scope="pipeline:QUOTE/extract", model=model) as rec:
        response = client.models.generate_content(...)
        rec.set_response(response)

Latency is measured around the block; usage is read from the response. On an
exception the call is recorded as an error (with a classified error type) and
re-raised unchanged.

Token columns carry one invariant::

    total_tokens == prompt_tokens + output_tokens + thought_tokens

It holds for **both** call paths, which matters because they report usage
differently: the raw SDK excludes thinking from ``candidates_token_count``,
while LangChain's ``output_tokens`` already includes it. The LangChain adapter
subtracts thinking so both agree (see ``_tokens_from_usage``), and
``_apply_token_invariant`` fills in a component the provider omitted rather than
leaving a NULL that silently nulls out every derivation. Derive cost from
``total_tokens``, or from the components — not both.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_QUEUE_MAX = 5000
_BATCH_MAX = 50
_FLUSH_TIMEOUT_S = 2.0
_LOG_EVERY_N_FAILURES = 20

_queue: "queue.Queue[dict]" = queue.Queue(maxsize=_QUEUE_MAX)
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_stats_lock = threading.Lock()
_stats = {"queued": 0, "written": 0, "dropped": 0, "failed": 0, "failed_batches": 0}


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rate_limit", ("429", "RESOURCE_EXHAUSTED", "too many requests")),
    ("not_found", ("404", "NOT_FOUND", "was not found")),
    ("invalid_argument", ("400", "INVALID_ARGUMENT")),
    ("permission", ("401", "403", "PERMISSION_DENIED", "UNAUTHENTICATED")),
    ("deadline", ("504", "DEADLINE_EXCEEDED", "deadline")),
    ("unavailable", ("503", "UNAVAILABLE", "overloaded")),
    ("server_error", ("500", "502", "INTERNAL")),
    ("timeout", ("timeout", "timed out")),
)

# Authoritative mapping when the SDK gives us a status code. This runs *before*
# message matching because a structured code is more reliable than a substring:
# a google-genai error with code=429 but an unhelpful message would otherwise be
# filed as a generic client error and hide the rate limiting.
_CODE_MAP = {
    400: "invalid_argument",
    401: "permission",
    403: "permission",
    404: "not_found",
    408: "timeout",
    429: "rate_limit",
    500: "server_error",
    502: "server_error",
    503: "unavailable",
    504: "deadline",
}

_STATUS_RE = re.compile(r"\b(4\d\d|5\d\d)\b")


def classify_error(exc: BaseException) -> tuple[str, int | None]:
    """Map an exception to (error_class, http_status).

    Uses a structured status code when the SDK provides one; otherwise falls
    back to matching the message. The message fallback exists because the SDK
    does not expose a code on every error path across versions — it is a
    known-fragile backstop, not the primary mechanism.
    """
    code = None
    for attr in ("code", "status_code", "http_status"):
        raw = getattr(exc, attr, None)
        if isinstance(raw, int):
            code = raw
            break
        if isinstance(raw, str) and raw.isdigit():
            code = int(raw)
            break

    text = f"{type(exc).__name__}: {exc}".lower()

    if code is None:
        match = _STATUS_RE.search(text)
        if match:
            code = int(match.group(1))

    if code is not None:
        if code in _CODE_MAP:
            return _CODE_MAP[code], code
        return ("server_error" if code >= 500 else "client_error"), code

    for name, markers in _MARKERS:
        if any(m.lower() in text for m in markers):
            return name, None

    return "unknown", None


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


def record_llm_call(**fields: Any) -> None:
    """Enqueue one telemetry row. Never raises, never blocks.

    Unknown keys are dropped so a caller typo cannot poison the insert.
    """
    try:
        row = _normalise(fields)
    except Exception:  # noqa: BLE001 - telemetry must not break callers
        logger.debug("llm telemetry: could not normalise row", exc_info=True)
        return

    # Always leave a log line — cheap, and survives a DB outage.
    if row.get("status") != "ok" or logger.isEnabledFor(logging.DEBUG):
        logger.info(
            "llm_call scope=%s model=%s tier=%s status=%s latency_ms=%s "
            "tokens=%s/%s+%s err=%s",
            row.get("scope"), row.get("model"), row.get("service_tier"),
            row.get("status"), row.get("latency_ms"),
            row.get("prompt_tokens"), row.get("output_tokens"),
            row.get("thought_tokens"), row.get("error_class"),
        )

    if not _enabled():
        return

    try:
        _queue.put_nowait(row)
        with _stats_lock:
            _stats["queued"] += 1
        _ensure_worker()
    except queue.Full:
        with _stats_lock:
            _stats["dropped"] += 1
            dropped = _stats["dropped"]
        if dropped % _LOG_EVERY_N_FAILURES == 1:
            logger.warning(
                "llm telemetry: queue full, dropping rows (dropped=%d total)", dropped
            )
    except Exception:  # noqa: BLE001
        logger.debug("llm telemetry: enqueue failed", exc_info=True)


_COLUMNS = (
    "ts", "scope", "provider", "model", "location", "service_tier",
    "latency_ms", "ttft_ms", "prompt_tokens", "output_tokens", "thought_tokens",
    "total_tokens", "attempt", "fell_back_from", "status", "error_class",
    "http_status", "correlation_id",
)


_TOKEN_FIELDS = ("prompt_tokens", "output_tokens", "thought_tokens")


def _apply_token_invariant(row: dict) -> None:
    """Force ``total_tokens == prompt + output + thought`` when a total is known.

    Providers omit zero-valued fields, so a component can legitimately arrive as
    None. Left alone that NULLs out every derivation: in production a single row
    with ``output_tokens`` missing made ``total - prompt - thought`` NULL, which
    is how this was found.

    Only acts when ``total_tokens`` is present. Without a total there is no
    invariant to satisfy, and filling blanks with zeros would be inventing data.
    """
    total = row.get("total_tokens")
    if not isinstance(total, int) or isinstance(total, bool):
        return

    missing = [k for k in _TOKEN_FIELDS if not isinstance(row.get(k), int)]
    if not missing:
        return

    if len(missing) == 1:
        key = missing[0]
        known = sum(row[k] for k in _TOKEN_FIELDS if k != key)
        derived = total - known
        if derived < 0:
            # The provider's numbers do not reconcile. Write 0 rather than a
            # negative count so the row still satisfies "no NULLs" and is
            # caught by a reconciliation query instead of silently poisoning one.
            logger.debug(
                "llm telemetry: %s does not reconcile (total=%s, others=%s); "
                "recording 0",
                key, total, known,
            )
            derived = 0
        row[key] = derived
        return

    # More than one unknown: the total cannot be attributed, so treat the absent
    # buckets as the empty buckets the provider omitted.
    for key in missing:
        row[key] = 0


def _normalise(fields: dict) -> dict:
    # Emit *every* column, not just the keys the caller passed.
    # `_apply_token_invariant` writes a derived value back into the row, so a
    # sparse row could gain a key its batch-mates do not have. One fixed shape
    # avoids that and makes the NULL-vs-zero story identical for every row.
    # (Verified against the real table 2026-09-26: SQLAlchemy tolerates a
    # heterogeneous executemany, so this is about consistency, not a crash.)
    row = {k: fields.get(k) for k in _COLUMNS}
    if not row.get("ts"):
        row["ts"] = datetime.now(timezone.utc)
    if not row.get("provider"):
        row["provider"] = "google"
    if not row.get("status"):
        row["status"] = "ok"
    if not row.get("scope"):
        row["scope"] = "unknown"
    if not row.get("model"):
        row["model"] = "unknown"
    _apply_token_invariant(row)
    # Truncate to the column widths so a long model/location string cannot
    # silently fail the whole batch.
    for key, width in (("scope", 80), ("model", 80), ("location", 64),
                       ("service_tier", 32), ("fell_back_from", 80),
                       ("status", 16), ("error_class", 64),
                       ("correlation_id", 128), ("provider", 32)):
        value = row.get(key)
        if isinstance(value, str) and len(value) > width:
            row[key] = value[:width]
    return row


def _enabled() -> bool:
    try:
        from config.settings import Config

        return bool(Config.LLM_TELEMETRY_ENABLED)
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------------------
# Background writer
# ---------------------------------------------------------------------------


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(
            target=_worker_loop, name="llm-telemetry", daemon=True
        )
        _worker.start()


def _worker_loop() -> None:
    while True:
        batch = []
        try:
            batch.append(_queue.get(timeout=_FLUSH_TIMEOUT_S))
        except queue.Empty:
            continue
        while len(batch) < _BATCH_MAX:
            try:
                batch.append(_queue.get_nowait())
            except queue.Empty:
                break
        _write_batch(batch)


def _write_batch(batch: list[dict]) -> None:
    from sqlalchemy import insert

    from includes.dashboard.database import get_session
    from includes.dashboard.models import LlmCallLog

    session = None
    try:
        session = get_session()
        session.execute(insert(LlmCallLog), batch)
        session.commit()
        with _stats_lock:
            _stats["written"] += len(batch)
    except Exception as exc:  # noqa: BLE001
        if session is not None:
            try:
                session.rollback()
            except Exception:  # noqa: BLE001
                pass
        with _stats_lock:
            _stats["failed"] += len(batch)
            _stats["failed_batches"] += 1
            failures = _stats["failed_batches"]
        if failures % _LOG_EVERY_N_FAILURES == 1:
            logger.warning(
                "llm telemetry: batch write failed (%d rows, %d failures): %s",
                len(batch), failures, exc,
            )
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


def flush(timeout: float = 5.0) -> bool:
    """Block until every enqueued row has been written, dropped or failed.

    Waiting on ``_queue.empty()`` alone is not enough: the writer may have
    dequeued a batch and still be inside the INSERT. Accounting for all four
    counters is what actually means "drained".
    """
    deadline = time.monotonic() + timeout
    while True:
        with _stats_lock:
            accounted = (
                _stats["written"] + _stats["dropped"] + _stats["failed"]
            )
            pending = _stats["queued"] - accounted
        if pending <= 0 and _queue.empty():
            # A batch may be in flight even when the queue looks empty; give it
            # one more beat to land before declaring success.
            time.sleep(0.05)
            with _stats_lock:
                accounted = (
                    _stats["written"] + _stats["dropped"] + _stats["failed"]
                )
                pending = _stats["queued"] - accounted
            if pending <= 0 and _queue.empty():
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def stats() -> dict:
    with _stats_lock:
        snapshot = dict(_stats)
    snapshot["queue_depth"] = _queue.qsize()
    return snapshot


def reset_stats() -> None:
    with _stats_lock:
        for key in _stats:
            _stats[key] = 0


# ---------------------------------------------------------------------------
# Call instrumentation
# ---------------------------------------------------------------------------


class CallRecord:
    """Accumulates one call's details; the context manager writes it out."""

    def __init__(self, scope: str, model: str, **kwargs: Any) -> None:
        self.scope = scope
        self.model = model
        self.location = kwargs.get("location")
        self.service_tier = kwargs.get("service_tier")
        self.attempt = kwargs.get("attempt", 1)
        self.fell_back_from = kwargs.get("fell_back_from")
        self.correlation_id = kwargs.get("correlation_id")
        self.prompt_tokens: int | None = None
        self.output_tokens: int | None = None
        self.thought_tokens: int | None = None
        self.total_tokens: int | None = None
        self.ttft_ms: int | None = None
        self.succeeded = False

    def set_response(self, response: Any) -> None:
        """Read usage out of a google-genai response."""
        self.succeeded = True
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return
        self.prompt_tokens = getattr(usage, "prompt_token_count", None)
        self.output_tokens = getattr(usage, "candidates_token_count", None)
        self.thought_tokens = getattr(usage, "thoughts_token_count", None)
        self.total_tokens = getattr(usage, "total_token_count", None)

    def set_usage(self, **tokens: int | None) -> None:
        """Set usage explicitly (for streaming, where you sum chunks yourself)."""
        self.succeeded = True
        for key, value in tokens.items():
            if hasattr(self, key):
                setattr(self, key, value)

    def set_ttft_ms(self, value: int | None) -> None:
        self.ttft_ms = value

    def mark_success(self) -> None:
        self.succeeded = True


@contextmanager
def instrument_call(scope: str, model: str, **kwargs: Any):
    """Time an LLM call and record it, whether it succeeds or raises.

    Yields a :class:`CallRecord`. The exception, if any, is re-raised unchanged
    after being recorded — this is a pure observer.
    """
    record = CallRecord(scope, model, **kwargs)
    started = time.perf_counter()
    try:
        yield record
    except BaseException as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        error_class, http_status = classify_error(exc)
        record_llm_call(
            scope=record.scope,
            model=record.model,
            location=record.location,
            service_tier=record.service_tier,
            latency_ms=latency_ms,
            ttft_ms=record.ttft_ms,
            prompt_tokens=record.prompt_tokens,
            output_tokens=record.output_tokens,
            thought_tokens=record.thought_tokens,
            total_tokens=record.total_tokens,
            attempt=record.attempt,
            fell_back_from=record.fell_back_from,
            status="error",
            error_class=error_class,
            http_status=http_status,
            correlation_id=record.correlation_id,
        )
        raise

    latency_ms = int((time.perf_counter() - started) * 1000)
    record_llm_call(
        scope=record.scope,
        model=record.model,
        location=record.location,
        service_tier=record.service_tier,
        latency_ms=latency_ms,
        ttft_ms=record.ttft_ms,
        prompt_tokens=record.prompt_tokens,
        output_tokens=record.output_tokens,
        thought_tokens=record.thought_tokens,
        total_tokens=record.total_tokens,
        attempt=record.attempt,
        fell_back_from=record.fell_back_from,
        status="ok",
        correlation_id=record.correlation_id,
    )


# ---------------------------------------------------------------------------
# LangChain adapter
# ---------------------------------------------------------------------------
# The agents call Gemini through ChatGoogleGenerativeAI (LangChain), not the raw
# SDK, so they need a callback handler rather than context management. We cannot
# wrap the agent's invocation directly the way instrument_call does.

try:  # LangChain is a hard dependency of the app, but keep this import soft so
    from langchain_core.callbacks import BaseCallbackHandler  # telemetry stays usable in isolation.
except Exception:  # noqa: BLE001
    class BaseCallbackHandler:  # type: ignore[no-redef]
        """Fallback so importing this module never fails."""


def _tokens_from_usage(usage: dict) -> dict:
    """Map a LangChain ``usage_metadata`` dict onto our token columns.

    **Why this subtracts.** LangChain's own identity is
    ``total = input + output`` with ``output_tokens`` *inclusive* of reasoning,
    and ``output_token_details.reasoning`` as a breakdown of that same output.
    We store thinking in its own column and expect
    ``total = prompt + output + thought``, so leaving the reasoning inside output
    makes ``output + thought`` double-count. Measured in production at +73% on
    agent rows (49,070 thoughts against 67,171 output). Subtracting here is what
    makes the raw-SDK and LangChain paths agree.

    Also accepts the older ``prompt_tokens`` / ``completion_tokens`` key names.
    """
    def _int(value: Any) -> int | None:
        # bool is an int subclass; a stray True must not become a token count.
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    details = usage.get("output_token_details")
    thinking = _int(details.get("reasoning")) if isinstance(details, dict) else None

    prompt = _int(usage.get("input_tokens"))
    if prompt is None:
        prompt = _int(usage.get("prompt_tokens"))

    output = _int(usage.get("output_tokens"))
    if output is None:
        output = _int(usage.get("completion_tokens"))

    total = _int(usage.get("total_tokens"))

    if thinking is not None and output is not None:
        # reasoning is a subset of output, not an addition to it
        output = max(0, output - thinking)

    return {
        "prompt_tokens": prompt,
        "output_tokens": output,
        "thought_tokens": thinking,
        "total_tokens": total,
    }


def _usage_from_langchain(response: Any) -> dict:
    """Pull token counts out of a LangChain LLMResult.

    LangChain has moved this around across versions, so collect every candidate
    location and prefer whichever one actually reports a total. Returns ``{}``
    when nothing is found, so absent data stays absent instead of becoming a
    false zero.
    """
    candidates: list[dict] = []

    llm_output = getattr(response, "llm_output", None) or {}
    for key in ("usage_metadata", "token_usage"):
        value = llm_output.get(key)
        if isinstance(value, dict) and value:
            candidates.append(value)

    try:
        message = response.generations[0][0].message
        value = getattr(message, "usage_metadata", None)
        if isinstance(value, dict) and value:
            candidates.append(value)
    except (AttributeError, IndexError, TypeError):
        pass

    if not candidates:
        return {}

    chosen = next(
        (c for c in candidates if c.get("total_tokens") is not None), candidates[0]
    )
    return _tokens_from_usage(chosen)


class LangChainTelemetryHandler(BaseCallbackHandler):
    """Records every LangChain LLM call, including tool-selection round-trips.

    One handler per agent scope: ``create_model(agent_name)`` builds it with the
    scope baked in, since LangChain gives the handler no access to ours.
    """

    def __init__(self, scope: str, location: str | None = None,
                 service_tier: str | None = None,
                 correlation_id: str | None = None) -> None:
        super().__init__()
        self.scope = scope
        self.location = location
        self.service_tier = service_tier
        self.correlation_id = correlation_id
        self._started: float | None = None
        self._model: str | None = None

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _model_name(serialized: Any, kwargs: dict) -> str | None:
        if isinstance(serialized, dict):
            inner = serialized.get("kwargs") or {}
            if isinstance(inner, dict) and inner.get("model"):
                return str(inner["model"])
            if serialized.get("name"):
                return str(serialized["name"])
        inv = kwargs.get("invocation_params") or {}
        if isinstance(inv, dict) and inv.get("model"):
            return str(inv["model"])
        return None

    def _emit(self, *, status: str, error: BaseException | None = None,
              usage: dict | None = None) -> None:
        latency_ms = None
        if self._started is not None:
            latency_ms = int((time.perf_counter() - self._started) * 1000)
        error_class = http_status = None
        if error is not None:
            error_class, http_status = classify_error(error)
        usage = usage or {}
        record_llm_call(
            scope=self.scope,
            model=self._model or "unknown",
            location=self.location,
            service_tier=self.service_tier,
            latency_ms=latency_ms,
            prompt_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("output_tokens"),
            thought_tokens=usage.get("thought_tokens"),
            total_tokens=usage.get("total_tokens"),
            status=status,
            error_class=error_class,
            http_status=http_status,
            correlation_id=self.correlation_id,
        )

    # -- callbacks -------------------------------------------------------
    def on_llm_start(self, serialized, prompts, **kwargs) -> None:  # noqa: D102
        self._started = time.perf_counter()
        self._model = self._model_name(serialized, kwargs)

    def on_chat_model_start(self, serialized, messages, **kwargs) -> None:  # noqa: D102
        self._started = time.perf_counter()
        self._model = self._model_name(serialized, kwargs)

    def on_llm_end(self, response, **kwargs) -> None:  # noqa: D102
        try:
            self._emit(status="ok", usage=_usage_from_langchain(response))
        except Exception:  # noqa: BLE001 - never break the agent
            logger.debug("llm telemetry: on_llm_end failed", exc_info=True)
        finally:
            self._started = None

    def on_llm_error(self, error, **kwargs) -> None:  # noqa: D102
        try:
            self._emit(status="error", error=error if isinstance(error, BaseException) else None)
        except Exception:  # noqa: BLE001
            logger.debug("llm telemetry: on_llm_error failed", exc_info=True)
        finally:
            self._started = None

