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


def _normalise(fields: dict) -> dict:
    row = {k: fields.get(k) for k in _COLUMNS if k in fields}
    row.setdefault("ts", datetime.now(timezone.utc))
    row.setdefault("provider", "google")
    row.setdefault("status", "ok")
    if not row.get("scope"):
        row["scope"] = "unknown"
    if not row.get("model"):
        row["model"] = "unknown"
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


def _usage_from_langchain(response: Any) -> dict:
    """Pull token counts out of a LangChain LLMResult.

    LangChain has moved this around across versions, so try, in order:
    ``llm_output['usage_metadata']``, then the first generation's message
    ``usage_metadata``. Returns whatever it finds; missing keys stay None
    rather than being guessed at.
    """
    found: dict = {}

    llm_output = getattr(response, "llm_output", None) or {}
    usage = llm_output.get("usage_metadata") or llm_output.get("token_usage") or {}
    if isinstance(usage, dict) and usage:
        found = {
            "prompt_tokens": usage.get("input_tokens") or usage.get("prompt_tokens"),
            "output_tokens": usage.get("output_tokens") or usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
        }

    if found.get("total_tokens") is None:
        try:
            message = response.generations[0][0].message
            usage = getattr(message, "usage_metadata", None) or {}
            if isinstance(usage, dict) and usage:
                details = usage.get("output_token_details") or {}
                found = {
                    "prompt_tokens": usage.get("input_tokens"),
                    "output_tokens": usage.get("output_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                    "thought_tokens": details.get("reasoning"),
                }
        except (AttributeError, IndexError, TypeError):
            pass

    return found


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

