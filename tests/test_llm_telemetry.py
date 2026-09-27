"""Tests for LLM telemetry (includes/llm/telemetry.py).

The contract that matters most: telemetry is a pure observer. A failure in the
sink must never propagate to the caller, and a caller's exception must be
re-raised unchanged after being recorded.
"""

from types import SimpleNamespace

import pytest

from includes.llm import telemetry


@pytest.fixture
def captured(monkeypatch):
    """Capture batches instead of writing to the database."""
    rows: list[dict] = []

    def fake_write(batch):
        rows.extend(batch)
        with telemetry._stats_lock:
            telemetry._stats["written"] += len(batch)

    monkeypatch.setattr(telemetry, "_write_batch", fake_write)
    # The suite disables telemetry globally; these tests exercise the sink.
    monkeypatch.setattr(telemetry, "_enabled", lambda: True)
    telemetry.reset_stats()
    return rows


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

class TestClassifyError:
    @pytest.mark.parametrize(
        "message,expected",
        [
            ("429 RESOURCE_EXHAUSTED. Resource exhausted.", "rate_limit"),
            ("404 NOT_FOUND. Publisher model was not found", "not_found"),
            ("400 INVALID_ARGUMENT. Bad request", "invalid_argument"),
            ("403 PERMISSION_DENIED", "permission"),
            ("504 DEADLINE_EXCEEDED", "deadline"),
            ("503 UNAVAILABLE. The model is overloaded", "unavailable"),
            ("500 INTERNAL server error", "server_error"),
            ("Read timed out", "timeout"),
        ],
    )
    def test_maps_messages(self, message, expected):
        error_class, _status = telemetry.classify_error(Exception(message))
        assert error_class == expected

    def test_extracts_http_status(self):
        error_class, status = telemetry.classify_error(
            Exception("404 NOT_FOUND. Publisher model was not found")
        )
        assert (error_class, status) == ("not_found", 404)

    def test_prefers_structured_code_attribute(self):
        exc = Exception("something obscure")
        exc.code = 429
        error_class, status = telemetry.classify_error(exc)
        assert error_class == "rate_limit"
        assert status == 429

    def test_unknown_error_is_not_guessed(self):
        error_class, status = telemetry.classify_error(Exception("banana"))
        assert error_class == "unknown"
        assert status is None


# ---------------------------------------------------------------------------
# The observer contract
# ---------------------------------------------------------------------------

class TestObserverContract:
    def test_records_success_with_usage(self, captured):
        with telemetry.instrument_call(
            scope="test:scope", model="gemini-x", location="global"
        ) as record:
            record.set_usage(prompt_tokens=10, output_tokens=5, total_tokens=15)

        assert telemetry.flush(5)
        row = captured[-1]
        assert row["scope"] == "test:scope"
        assert row["model"] == "gemini-x"
        assert row["location"] == "global"
        assert row["status"] == "ok"
        assert row["prompt_tokens"] == 10
        assert row["total_tokens"] == 15
        assert row["latency_ms"] is not None

    def test_records_error_and_reraises_unchanged(self, captured):
        sentinel = ValueError("429 RESOURCE_EXHAUSTED")

        with pytest.raises(ValueError) as caught:
            with telemetry.instrument_call(scope="test:scope", model="gemini-x"):
                raise sentinel

        assert caught.value is sentinel, "the original exception must propagate"
        assert telemetry.flush(5)
        row = captured[-1]
        assert row["status"] == "error"
        assert row["error_class"] == "rate_limit"

    def test_reading_usage_from_a_gemini_response(self, captured):
        usage = SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=20,
            thoughts_token_count=300,
            total_token_count=420,
        )
        with telemetry.instrument_call(scope="test:scope", model="gemini-x") as record:
            record.set_response(SimpleNamespace(usage_metadata=usage))

        assert telemetry.flush(5)
        row = captured[-1]
        assert row["prompt_tokens"] == 100
        assert row["output_tokens"] == 20
        # Thinking bills at the output rate, so it must be kept separate.
        assert row["thought_tokens"] == 300
        assert row["total_tokens"] == 420

    def test_records_even_when_the_call_returns_nothing(self, captured):
        with telemetry.instrument_call(scope="test:scope", model="gemini-x"):
            pass
        assert telemetry.flush(5)
        assert captured[-1]["status"] == "ok"

    def test_a_database_failure_never_reaches_the_caller(self, monkeypatch):
        """The whole point: telemetry is best-effort."""
        import includes.dashboard.database as db

        def boom():
            raise RuntimeError("database is down")

        monkeypatch.setattr(db, "get_session", boom)
        monkeypatch.setattr(telemetry, "_enabled", lambda: True)
        telemetry.reset_stats()

        # Must not raise, on either path.
        telemetry.record_llm_call(scope="test:scope", model="gemini-x")
        with telemetry.instrument_call(scope="test:scope", model="gemini-x"):
            pass

        assert telemetry.flush(5)
        stats = telemetry.stats()
        assert stats["failed"] >= 1
        assert stats["dropped"] == 0, "a write failure is not a drop"

    def test_a_full_queue_drops_without_raising(self, monkeypatch):
        import queue as queue_module

        monkeypatch.setattr(telemetry, "_queue", queue_module.Queue(maxsize=1))
        monkeypatch.setattr(telemetry, "_ensure_worker", lambda: None)
        monkeypatch.setattr(telemetry, "_enabled", lambda: True)
        telemetry.reset_stats()

        telemetry.record_llm_call(scope="first", model="gemini-x")
        telemetry.record_llm_call(scope="second", model="gemini-x")  # no room

        assert telemetry.stats()["dropped"] == 1

    def test_overlong_values_are_truncated_not_rejected(self, captured):
        telemetry.record_llm_call(scope="s" * 200, model="m" * 200)
        assert telemetry.flush(5)
        row = captured[-1]
        assert len(row["scope"]) == 80
        assert len(row["model"]) == 80


# ---------------------------------------------------------------------------
# LangChain adapter
# ---------------------------------------------------------------------------

class TestLangChainAdapter:
    def test_usage_from_llm_output(self):
        response = SimpleNamespace(
            llm_output={
                "usage_metadata": {
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "total_tokens": 7,
                }
            },
            generations=[],
        )
        usage = telemetry._usage_from_langchain(response)
        assert usage["prompt_tokens"] == 3
        assert usage["output_tokens"] == 4
        assert usage["total_tokens"] == 7

    def test_usage_from_message_fallback_including_reasoning(self):
        message = SimpleNamespace(
            usage_metadata={
                "input_tokens": 5,
                "output_tokens": 6,
                "total_tokens": 11,
                "output_token_details": {"reasoning": 2},
            }
        )
        response = SimpleNamespace(
            llm_output={}, generations=[[SimpleNamespace(message=message)]]
        )
        usage = telemetry._usage_from_langchain(response)
        assert usage["prompt_tokens"] == 5
        assert usage["total_tokens"] == 11
        assert usage["thought_tokens"] == 2
        # LangChain's output_tokens is inclusive of reasoning, so 6 -> 6 - 2.
        assert usage["output_tokens"] == 4

    def test_missing_usage_is_none_not_zero(self):
        """Absent data must stay absent so we never report a false zero."""
        response = SimpleNamespace(llm_output={}, generations=[])
        assert telemetry._usage_from_langchain(response) == {}

    def test_handler_records_a_completed_call(self, captured):
        handler = telemetry.LangChainTelemetryHandler(scope="agent:test")
        handler.on_llm_start({"kwargs": {"model": "gemini-x"}}, ["hello"])
        handler.on_llm_end(
            SimpleNamespace(
                llm_output={
                    "usage_metadata": {
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "total_tokens": 3,
                    }
                },
                generations=[],
            )
        )

        assert telemetry.flush(5)
        row = captured[-1]
        assert row["scope"] == "agent:test"
        assert row["model"] == "gemini-x"
        assert row["status"] == "ok"
        assert row["total_tokens"] == 3

    def test_handler_records_a_failed_call(self, captured):
        handler = telemetry.LangChainTelemetryHandler(scope="agent:test")
        handler.on_chat_model_start({"kwargs": {"model": "gemini-x"}}, [[]])
        handler.on_llm_error(ValueError("429 RESOURCE_EXHAUSTED"))

        assert telemetry.flush(5)
        row = captured[-1]
        assert row["status"] == "error"
        assert row["error_class"] == "rate_limit"

    def test_handler_swallows_its_own_failures(self, monkeypatch):
        """A broken handler must not break the agent."""
        handler = telemetry.LangChainTelemetryHandler(scope="agent:test")

        def boom(*_args, **_kwargs):
            raise RuntimeError("telemetry exploded")

        monkeypatch.setattr(telemetry, "record_llm_call", boom)
        handler.on_llm_start({}, [])
        handler.on_llm_end(SimpleNamespace(llm_output={}, generations=[]))
        handler.on_llm_error(ValueError("nope"))


# ---------------------------------------------------------------------------
# The token invariant: total = prompt + output + thought, on both paths
# ---------------------------------------------------------------------------

class TestTokenInvariant:
    def test_raw_sdk_and_langchain_agree_on_the_same_call(self):
        """One call, two adapters, identical token columns.

        This is the regression that let the original mismatch through: the raw
        SDK excludes thinking from `candidates_token_count`, LangChain's
        `output_tokens` includes it, and nothing ever compared the two. The
        production consequence was `output + thought` double-counting at +73%
        on agent rows.
        """
        # One call: 100 prompt, 50 output of which 20 was thinking, 150 total.
        sdk_record = telemetry.CallRecord(scope="pipeline:X/y", model="gemini-x")
        sdk_record.set_response(
            SimpleNamespace(
                usage_metadata=SimpleNamespace(
                    prompt_token_count=100,
                    candidates_token_count=30,  # thinking is excluded here
                    thoughts_token_count=20,
                    total_token_count=150,
                )
            )
        )
        sdk = {
            "prompt_tokens": sdk_record.prompt_tokens,
            "output_tokens": sdk_record.output_tokens,
            "thought_tokens": sdk_record.thought_tokens,
            "total_tokens": sdk_record.total_tokens,
        }

        lc = telemetry._usage_from_langchain(
            SimpleNamespace(
                llm_output={
                    "usage_metadata": {
                        "input_tokens": 100,
                        "output_tokens": 50,  # thinking is included here
                        "total_tokens": 150,
                        "output_token_details": {"reasoning": 20},
                    }
                },
                generations=[],
            )
        )

        assert sdk == lc, f"adapters disagree: sdk={sdk} langchain={lc}"
        for label, tokens in (("raw-sdk", sdk), ("langchain", lc)):
            assert tokens["prompt_tokens"] + tokens["output_tokens"] + tokens[
                "thought_tokens"
            ] == tokens["total_tokens"], label

    def test_langchain_reasoning_is_not_reported_twice(self):
        usage = telemetry._tokens_from_usage(
            {
                "input_tokens": 10,
                "output_tokens": 40,
                "total_tokens": 50,
                "output_token_details": {"reasoning": 25},
            }
        )
        assert usage["thought_tokens"] == 25
        assert usage["output_tokens"] == 15
        assert (
            usage["prompt_tokens"] + usage["output_tokens"] + usage["thought_tokens"]
            == usage["total_tokens"]
        )

    def test_invariant_fills_a_component_the_provider_omitted(self):
        """The production row: a missing output_tokens nulled every derivation.

        2026-09-24 21:09:48 pipeline:QUOTE/extract had prompt=1099, output=NULL,
        thought=109, total=1208 — so `total - prompt - thought` was NULL.
        """
        row = telemetry._normalise(
            {
                "scope": "pipeline:QUOTE/extract",
                "model": "gemini-3.8-flash",
                "prompt_tokens": 1099,
                "output_tokens": None,
                "thought_tokens": 109,
                "total_tokens": 1208,
            }
        )
        assert row["output_tokens"] == 0
        assert (
            row["prompt_tokens"] + row["output_tokens"] + row["thought_tokens"]
            == row["total_tokens"]
        )

    def test_invariant_does_not_invent_tokens_without_a_total(self):
        """No total means no invariant to satisfy — absent must stay absent."""
        row = telemetry._normalise(
            {"scope": "s", "model": "m", "prompt_tokens": 10}
        )
        assert row["output_tokens"] is None
        assert row["thought_tokens"] is None

    def test_every_row_carries_the_same_columns(self):
        """Every row must have an identical shape.

        `_apply_token_invariant` writes into the row, so a sparse row could gain
        a key its batch-mates lack. SQLAlchemy tolerates that (verified against
        the real table), but one fixed shape keeps the NULL-vs-zero story
        uniform and the invariant check meaningful.
        """
        sparse = telemetry._normalise({"scope": "s", "model": "m"})
        full = telemetry._normalise(
            {
                "scope": "s",
                "model": "m",
                "latency_ms": 1,
                "prompt_tokens": 1,
                "output_tokens": 2,
                "thought_tokens": 0,
                "total_tokens": 3,
            }
        )
        assert set(sparse) == set(full) == set(telemetry._COLUMNS)
