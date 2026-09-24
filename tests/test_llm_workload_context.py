"""Tests for the ambient LLM workload context (includes/llm/context.py).

The mechanism only works if a contextvar set in a background loop coroutine
reaches the sync worker thread that the pipeline actually runs in — main.py
calls every loop body through ``asyncio.to_thread``. If that stopped holding,
background work would silently revert to the interactive model and tier with no
error anywhere. So the propagation is tested explicitly.
"""

import asyncio
import threading

import pytest

from includes.llm import context
from includes.llm.context import INTERACTIVE, SYNC, is_sync, workload


class TestWorkloadContext:
    def test_defaults_to_interactive(self):
        assert context.current_workload() == INTERACTIVE
        assert not is_sync()

    def test_sets_and_restores(self):
        with workload(SYNC):
            assert is_sync()
        assert not is_sync()

    def test_restores_after_an_exception(self):
        with pytest.raises(ValueError):
            with workload(SYNC):
                raise ValueError("boom")
        assert not is_sync()

    def test_nesting_restores_the_outer_value(self):
        with workload(SYNC):
            with workload(INTERACTIVE):
                assert not is_sync()
            assert is_sync()
        assert not is_sync()

    def test_propagates_into_asyncio_to_thread(self):
        """main.py runs every loop body through to_thread, so this must hold."""
        seen: dict = {}

        def probe():
            seen["workload"] = context.current_workload()
            return context.is_sync()

        async def run():
            with workload(SYNC):
                return await asyncio.to_thread(probe)

        assert asyncio.run(run()) is True
        assert seen["workload"] == SYNC

    def test_an_unrelated_thread_does_not_inherit_sync(self):
        """A plain thread gets a fresh context — we must not leak sync into it."""
        seen: dict = {}

        def probe():
            seen["workload"] = context.current_workload()

        with workload(SYNC):
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join()

        assert seen["workload"] == INTERACTIVE


class TestTierResolution:
    def test_unconfigured_means_no_tier_requested(self, monkeypatch):
        """Inert by default: omitting service_tier is Standard behaviour."""
        from config.settings import Config

        monkeypatch.setattr(Config, "INTERACTIVE_SERVICE_TIER", "", raising=False)
        monkeypatch.setattr(Config, "SYNC_SERVICE_TIER", "", raising=False)

        assert context.current_service_tier() is None
        with workload(SYNC):
            assert context.current_service_tier() is None

    def test_sync_and_interactive_resolve_independently(self, monkeypatch):
        from config.settings import Config

        monkeypatch.setattr(
            Config, "INTERACTIVE_SERVICE_TIER", "SERVICE_TIER_PRIORITY", raising=False
        )
        monkeypatch.setattr(
            Config, "SYNC_SERVICE_TIER", "SERVICE_TIER_FLEX", raising=False
        )

        assert context.current_service_tier() == "SERVICE_TIER_PRIORITY"
        with workload(SYNC):
            assert context.current_service_tier() == "SERVICE_TIER_FLEX"


class TestModelResolution:
    def test_sync_substitutes_sync_model_when_nothing_explicit(self, monkeypatch):
        """The actual fix: a pipeline inheriting DEFAULT_MODEL is the shared-with-chat case."""
        import includes.email_pipeline as ep
        from config.settings import Config

        monkeypatch.setattr(Config, "SYNC_MODEL", "sync-model", raising=False)
        monkeypatch.setattr(Config, "DEFAULT_MODEL", "default-model", raising=False)
        monkeypatch.delenv("TESTP_SOMESTEP_MODEL", raising=False)
        monkeypatch.delenv("TESTP_PIPELINE_MODEL", raising=False)

        assert ep.get_pipeline_model("TESTP", "somestep") == "default-model"
        with workload(SYNC):
            assert ep.get_pipeline_model("TESTP", "somestep") == "sync-model"

    def test_explicit_step_model_wins_even_in_sync(self, monkeypatch):
        """Explicit config is a deliberate quality choice — background-ness must not override it."""
        import includes.email_pipeline as ep
        from config.settings import Config

        monkeypatch.setattr(Config, "SYNC_MODEL", "sync-model", raising=False)
        monkeypatch.setenv("TESTP_SOMESTEP_MODEL", "chosen-step-model")

        with workload(SYNC):
            assert ep.get_pipeline_model("TESTP", "somestep") == "chosen-step-model"

    def test_explicit_pipeline_model_wins_even_in_sync(self, monkeypatch):
        import includes.email_pipeline as ep
        from config.settings import Config

        monkeypatch.setattr(Config, "SYNC_MODEL", "sync-model", raising=False)
        monkeypatch.delenv("TESTP_SOMESTEP_MODEL", raising=False)
        monkeypatch.setenv("TESTP_PIPELINE_MODEL", "chosen-pipeline-model")

        with workload(SYNC):
            assert ep.get_pipeline_model("TESTP", "somestep") == "chosen-pipeline-model"

    def test_sync_scope_is_prefixed_in_telemetry(self, monkeypatch):
        """Background calls must be separable from user-triggered ones in the log."""
        import includes.email_pipeline as ep
        from includes.llm import telemetry

        captured: list[dict] = []

        def fake_write(batch):
            # Must bump the accounting counter too — flush() waits for
            # written + dropped + failed to cover everything queued.
            captured.extend(batch)
            with telemetry._stats_lock:
                telemetry._stats["written"] += len(batch)

        monkeypatch.setattr(telemetry, "_write_batch", fake_write)
        monkeypatch.setattr(telemetry, "_enabled", lambda: True)
        monkeypatch.setattr(ep, "get_pipeline_candidates", lambda p, s: ["m"])
        telemetry.reset_stats()

        class FakeResponse:
            usage_metadata = None

        class FakeModels:
            def generate_content(self, **_kwargs):
                return FakeResponse()

        class FakeClient:
            def __init__(self, **_kwargs):
                self.models = FakeModels()

        monkeypatch.setattr("google.genai.Client", FakeClient)
        monkeypatch.setattr(ep.Config, "LLM_MAX_ATTEMPT_SECONDS", 5, raising=False)

        ep.llm_call_with_retry("TESTP", "somestep", ["x"])
        with workload(SYNC):
            ep.llm_call_with_retry("TESTP", "somestep", ["x"])

        assert telemetry.flush(5)
        scopes = [row["scope"] for row in captured]
        assert "pipeline:TESTP/somestep" in scopes
        assert "sync:TESTP/somestep" in scopes
