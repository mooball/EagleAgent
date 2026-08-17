"""
Unit tests for includes/tools/job_tools.py — LangGraph tool wrappers for job management.
"""

import asyncio
import importlib
from collections import deque
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from includes.job_runner import Job, JobRunner


# ============================================================================
# Helpers
# ============================================================================

def _make_job(
    job_id="test-1234-abcd",
    script_name="update_product_embeddings",
    status="running",
    pid=9999,
    output=None,
    **kwargs,
):
    """Create a Job dataclass with sensible defaults."""
    return Job(
        id=job_id,
        script_name=script_name,
        command=["uv", "run", "python", "-m", f"scripts.{script_name}"],
        status=status,
        started_at=datetime.now(timezone.utc),
        pid=pid,
        output=deque(output or [], maxlen=200),
        **kwargs,
    )


def _create_tools(runner):
    """Create job tools bound to a runner. No Chainlit involved any more."""
    import includes.tools.job_tools as mod
    return mod.create_job_tools(runner), None


# ============================================================================
# Tool creation
# ============================================================================

class TestCreateJobTools:
    def test_returns_five_tools(self):
        runner = MagicMock(spec=JobRunner)
        tools, _ = _create_tools(runner)
        assert len(tools) == 5

    def test_tool_names(self):
        runner = MagicMock(spec=JobRunner)
        tools, _ = _create_tools(runner)
        names = {t.name for t in tools}
        assert names == {"run_script", "list_scripts", "list_jobs", "get_job_status", "cancel_job"}


# ============================================================================
# run_script tool
# ============================================================================

class TestRunScript:
    async def test_run_script_sends_confirmation(self, bound_chat_ctx):
        """run_script should send a confirmation message, not start the job."""
        runner = MagicMock(spec=JobRunner)
        tools, _ = _create_tools(runner)
        run_tool = next(t for t in tools if t.name == "run_script")
        result = await run_tool.ainvoke({"script_name": "update_product_embeddings"})

        assert "Confirmation requested" in result
        assert "update_product_embeddings" in result
        # Job should NOT have been started
        runner.run_script.assert_not_called()
        # A confirmation message with Run / Cancel should have been sent
        assert len(bound_chat_ctx.messages) == 1
        assert bound_chat_ctx.action_names == ["confirm_run_script", "cancel_run_script"]

    async def test_run_script_unknown_script(self, bound_chat_ctx):
        runner = MagicMock(spec=JobRunner)
        tools, _ = _create_tools(runner)
        run_tool = next(t for t in tools if t.name == "run_script")
        result = await run_tool.ainvoke({"script_name": "nonexistent_script"})

        assert "Error" in result
        assert "Unknown script" in result
        assert bound_chat_ctx.messages == []


# ============================================================================
# list_scripts tool
# ============================================================================

class TestListScripts:
    async def test_list_scripts_shows_registry(self):
        runner = MagicMock(spec=JobRunner)
        tools, _ = _create_tools(runner)
        list_tool = next(t for t in tools if t.name == "list_scripts")
        result = await list_tool.ainvoke({})
        assert "update_product_embeddings" in result
        assert "sync_netsuite_suppliers" in result


# ============================================================================
# list_jobs tool
# ============================================================================

class TestListJobs:
    async def test_list_jobs_empty(self):
        runner = MagicMock(spec=JobRunner)
        runner.list_jobs.return_value = []
        tools, _ = _create_tools(runner)
        list_tool = next(t for t in tools if t.name == "list_jobs")
        result = await list_tool.ainvoke({})
        assert "No jobs" in result
    async def test_list_jobs_with_data(self):
        runner = MagicMock(spec=JobRunner)
        finished = datetime.now(timezone.utc)
        job = _make_job(status="completed", finished_at=finished)
        runner.list_jobs.return_value = [job]
        tools, _ = _create_tools(runner)
        list_tool = next(t for t in tools if t.name == "list_jobs")
        result = await list_tool.ainvoke({})
        assert "update_product_embeddings" in result
        assert "completed" in result


# ============================================================================
# get_job_status tool
# ============================================================================

class TestGetJobStatus:
    async def test_get_status_by_job_id(self):
        runner = MagicMock(spec=JobRunner)
        job = _make_job(output=["line 1", "line 2", "line 3"])
        runner.get_job.return_value = job
        tools, _ = _create_tools(runner)
        status_tool = next(t for t in tools if t.name == "get_job_status")
        result = await status_tool.ainvoke({"job_id": job.id})
        assert "running" in result
        assert "9999" in result
        assert "line 3" in result
    async def test_get_status_not_found(self):
        runner = MagicMock(spec=JobRunner)
        runner.get_job.return_value = None
        runner.list_jobs.return_value = []
        tools, _ = _create_tools(runner)
        status_tool = next(t for t in tools if t.name == "get_job_status")
        result = await status_tool.ainvoke({"job_id": "nonexistent"})
        assert "No job found" in result
    async def test_get_status_partial_id(self):
        runner = MagicMock(spec=JobRunner)
        job = _make_job(job_id="abcd1234-full-uuid")
        runner.get_job.return_value = None  # full ID lookup fails
        runner.list_jobs.return_value = [job]
        tools, _ = _create_tools(runner)
        status_tool = next(t for t in tools if t.name == "get_job_status")
        result = await status_tool.ainvoke({"job_id": "abcd1234"})
        assert "running" in result
    async def test_get_status_by_script_name(self):
        """Agent can look up a job by script name when it doesn't have the job ID."""
        runner = MagicMock(spec=JobRunner)
        job = _make_job(script_name="update_supplier_embeddings")
        runner.get_job.return_value = None
        runner.list_jobs.return_value = [job]
        tools, _ = _create_tools(runner)
        status_tool = next(t for t in tools if t.name == "get_job_status")
        result = await status_tool.ainvoke({"script_name": "update_supplier_embeddings"})
        assert "running" in result
        assert "update_supplier_embeddings" in result
    async def test_get_status_no_args(self):
        """If neither job_id nor script_name is given, prompt the user."""
        runner = MagicMock(spec=JobRunner)
        tools, _ = _create_tools(runner)
        status_tool = next(t for t in tools if t.name == "get_job_status")
        result = await status_tool.ainvoke({})
        assert "list_jobs" in result


# ============================================================================
# cancel_job tool
# ============================================================================

class TestCancelJob:
    async def test_cancel_success(self):
        runner = MagicMock(spec=JobRunner)
        job = _make_job()
        runner.get_job.return_value = job
        runner.cancel = AsyncMock(return_value=job)
        tools, _ = _create_tools(runner)
        cancel_tool = next(t for t in tools if t.name == "cancel_job")
        result = await cancel_tool.ainvoke({"job_id": job.id})
        assert "Cancelled" in result
        runner.cancel.assert_awaited_once()
    async def test_cancel_not_found(self):
        runner = MagicMock(spec=JobRunner)
        runner.get_job.return_value = None
        runner.list_jobs.return_value = []
        runner.cancel = AsyncMock(side_effect=ValueError("Unknown job: nope"))
        tools, _ = _create_tools(runner)
        cancel_tool = next(t for t in tools if t.name == "cancel_job")
        result = await cancel_tool.ainvoke({"job_id": "nope"})
        assert "Error" in result


# ============================================================================
# Script awareness in prompts
# ============================================================================

class TestScriptAwareness:
    def test_admin_sees_script_section(self):
        from includes.prompts import _build_script_awareness
        result = _build_script_awareness({"role": "Admin"})
        assert "list_jobs" in result
        assert "update_product_embeddings" in result
        assert "ALWAYS call" in result

    def test_non_admin_sees_nothing(self):
        from includes.prompts import _build_script_awareness
        result = _build_script_awareness({"role": "Staff"})
        assert result == ""

    def test_none_profile_sees_nothing(self):
        from includes.prompts import _build_script_awareness
        result = _build_script_awareness(None)
        assert result == ""


# ============================================================================
# Signal handling (task #6)
# ============================================================================

class TestSignalHandling:
    async def test_start_creates_reaper_task(self):
        """JobRunner.start() should create a background reaper task."""
        runner = JobRunner()
        assert runner._reaper_task is None

        await runner.start()
        try:
            assert runner._reaper_task is not None
            assert not runner._reaper_task.done()
        finally:
            await runner.shutdown()
    async def test_shutdown_cancels_reaper(self):
        """shutdown() should cancel the reaper task."""
        runner = JobRunner()
        await runner.start()
        reaper = runner._reaper_task
        assert reaper is not None

        await runner.shutdown()
        assert reaper.done()
