"""
Unit tests for includes/job_runner.py — async background job runner.

Uses a real subprocess (echo / sleep) to exercise the full lifecycle:
start, track status, capture output, cancel, duplicate rejection, shutdown.
"""

import asyncio
import sys

import pytest

from includes.job_runner import Job, JobRunner


# ============================================================================
# Helpers
# ============================================================================

# Temporary script entries used only during tests.  We patch the registry
# so that JobRunner.run_script() finds them without touching production config.
_TEST_SCRIPTS = {
    "_test_echo": {
        "command": [sys.executable, "-c", "print('hello world')"],
        "description": "Test script that prints one line",
        "args_allowed": [],
        "long_running": False,
    },
    "_test_sleep": {
        "command": [sys.executable, "-c", "import time; time.sleep(30)"],
        "description": "Test script that sleeps (for cancel tests)",
        "args_allowed": [],
        "long_running": True,
    },
    "_test_fail": {
        "command": [sys.executable, "-c", "import sys; print('oops'); sys.exit(2)"],
        "description": "Test script that exits with code 2",
        "args_allowed": [],
        "long_running": False,
    },
}


@pytest.fixture(autouse=True)
def _patch_registry(monkeypatch):
    """Inject test scripts into the registry for every test in this module."""
    import config.scripts as scripts_mod
    original = dict(scripts_mod.SCRIPT_REGISTRY)
    scripts_mod.SCRIPT_REGISTRY.update(_TEST_SCRIPTS)
    yield
    # Restore original registry
    scripts_mod.SCRIPT_REGISTRY.clear()
    scripts_mod.SCRIPT_REGISTRY.update(original)


@pytest.fixture
async def runner():
    """Create a started JobRunner, shut it down after the test."""
    r = JobRunner()
    await r.start()
    yield r
    await r.shutdown()


# ============================================================================
# Lifecycle
# ============================================================================

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_creates_reaper(self, runner):
        assert runner._reaper_task is not None
        assert not runner._reaper_task.done()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_reaper(self):
        r = JobRunner()
        await r.start()
        task = r._reaper_task
        await r.shutdown()
        assert task.done()


# ============================================================================
# run_script
# ============================================================================

class TestRunScript:
    @pytest.mark.asyncio
    async def test_successful_run(self, runner):
        job = await runner.run_script("_test_echo")
        assert job.status == "running"
        assert job.pid is not None
        assert job.script_name == "_test_echo"

        # Wait for the process to finish
        await job._process.wait()
        # Let the reaper pick it up
        await asyncio.sleep(3)

        updated = runner.get_job(job.id)
        assert updated.status == "completed"
        assert updated.exit_code == 0
        assert any("hello world" in line for line in updated.output)

    @pytest.mark.asyncio
    async def test_failed_script(self, runner):
        job = await runner.run_script("_test_fail")
        await job._process.wait()
        await asyncio.sleep(3)

        updated = runner.get_job(job.id)
        assert updated.status == "failed"
        assert updated.exit_code == 2
        assert any("oops" in line for line in updated.output)

    @pytest.mark.asyncio
    async def test_unknown_script_raises(self, runner):
        with pytest.raises(ValueError, match="Unknown script"):
            await runner.run_script("nonexistent_script_xyz")

    @pytest.mark.asyncio
    async def test_duplicate_run_rejected(self, runner):
        job = await runner.run_script("_test_sleep")
        try:
            with pytest.raises(ValueError, match="already running"):
                await runner.run_script("_test_sleep")
        finally:
            await runner.cancel(job.id)

    @pytest.mark.asyncio
    async def test_thread_id_stored(self, runner):
        job = await runner.run_script("_test_echo", thread_id="thread-123")
        assert job.thread_id == "thread-123"
        await job._process.wait()


# ============================================================================
# Output capture
# ============================================================================

class TestOutputCapture:
    @pytest.mark.asyncio
    async def test_output_captured(self, runner):
        job = await runner.run_script("_test_echo")
        await job._process.wait()
        # Give the stream reader a moment to finish
        await asyncio.sleep(0.5)
        assert len(job.output) > 0
        assert "hello world" in list(job.output)[0]


# ============================================================================
# Cancel
# ============================================================================

class TestCancel:
    @pytest.mark.asyncio
    async def test_cancel_running_job(self, runner):
        job = await runner.run_script("_test_sleep")
        result = await runner.cancel(job.id)
        assert result.status == "cancelled"
        assert result.finished_at is not None

    @pytest.mark.asyncio
    async def test_cancel_unknown_job_raises(self, runner):
        with pytest.raises(ValueError, match="Unknown job"):
            await runner.cancel("does-not-exist")

    @pytest.mark.asyncio
    async def test_cancel_finished_job_raises(self, runner):
        job = await runner.run_script("_test_echo")
        await job._process.wait()
        await asyncio.sleep(3)  # Let reaper mark it completed
        with pytest.raises(ValueError, match="not running"):
            await runner.cancel(job.id)


# ============================================================================
# get_job / list_jobs
# ============================================================================

class TestQuery:
    @pytest.mark.asyncio
    async def test_get_job(self, runner):
        job = await runner.run_script("_test_echo")
        assert runner.get_job(job.id) is job
        await job._process.wait()

    @pytest.mark.asyncio
    async def test_get_job_not_found(self, runner):
        assert runner.get_job("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_jobs(self, runner):
        job = await runner.run_script("_test_echo")
        jobs = runner.list_jobs()
        assert len(jobs) >= 1
        assert any(j.id == job.id for j in jobs)
        await job._process.wait()


# ============================================================================
# Script registry validation
# ============================================================================

class TestScriptRegistryValidation:
    def test_get_script_known(self):
        from config.scripts import get_script
        assert get_script("_test_echo") is not None

    def test_get_script_unknown(self):
        from config.scripts import get_script
        assert get_script("does_not_exist") is None

    def test_validate_args_empty(self):
        from config.scripts import validate_args
        result = validate_args("_test_echo", [])
        assert result == []

    def test_validate_args_rejects_unknown_flag(self):
        from config.scripts import validate_args
        with pytest.raises(ValueError, match="not allowed"):
            validate_args("_test_echo", ["--sneaky"])

    def test_validate_args_unknown_script(self):
        from config.scripts import validate_args
        with pytest.raises(ValueError, match="Unknown script"):
            validate_args("no_such_script", [])

    def test_validate_args_allowed_flag(self):
        from config.scripts import validate_args
        result = validate_args("import_suppliers", ["--phase"])
        assert result == ["--phase"]
