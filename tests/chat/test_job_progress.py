"""monitor_job posts job lifecycle messages through the ChatContext.

This module was at 0% coverage before the Phase 1 conversion.
"""

from collections import deque
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from includes.chat.job_progress import monitor_job
from includes.job_runner import Job, JobRunner


def _job(status="completed", output=None, **kwargs):
    started = datetime.now(timezone.utc)
    defaults = {
        "id": "abcd1234-5678-uuid",
        "script_name": "update_product_embeddings",
        "command": ["uv", "run", "python", "-m", "scripts.update_product_embeddings"],
        "status": status,
        "started_at": started,
        "finished_at": started + timedelta(seconds=95),
        "pid": 4242,
        "output": deque(output or [], maxlen=200),
    }
    defaults.update(kwargs)
    return Job(**defaults)


@pytest.fixture
def runner():
    return MagicMock(spec=JobRunner)


@pytest.mark.asyncio
async def test_start_message_carries_a_cancel_button(runner, chat_ctx):
    job = _job(output=["done"])
    await monitor_job(runner, job, chat_ctx)

    start = chat_ctx.messages[0]
    assert "**Started**" in start.content
    assert "update_product_embeddings" in start.content
    assert "abcd1234" in start.content
    assert "4242" in start.content

    assert [a.name for a in start.actions] == ["cancel_job"]
    assert start.actions[0].payload == {"job_id": "abcd1234-5678-uuid"}
    assert start.actions[0].label == "Cancel"


@pytest.mark.asyncio
async def test_completed_job_reports_duration_and_output_tail(runner, chat_ctx):
    job = _job(status="completed", output=["a", "b", "c", "d", "e"])
    await monitor_job(runner, job, chat_ctx)

    final = chat_ctx.messages[-1]
    assert "**Completed**" in final.content
    assert "0:01:35" in final.content
    # Only the last three lines
    assert "c\nd\ne" in final.content
    assert "a" not in final.content.split("```")[1]


@pytest.mark.asyncio
async def test_failed_job_reports_exit_code(runner, chat_ctx):
    job = _job(status="failed", exit_code=2, output=["boom"])
    await monitor_job(runner, job, chat_ctx)

    final = chat_ctx.messages[-1]
    assert "**Failed**" in final.content
    assert "exit code 2" in final.content
    assert "boom" in final.content


@pytest.mark.asyncio
async def test_cancelled_job_reports_cancellation(runner, chat_ctx):
    job = _job(status="cancelled")
    await monitor_job(runner, job, chat_ctx)

    final = chat_ctx.messages[-1]
    assert "**Cancelled**" in final.content
    assert "update_product_embeddings" in final.content


@pytest.mark.asyncio
async def test_no_output_renders_a_placeholder(runner, chat_ctx):
    job = _job(status="completed", output=[])
    await monitor_job(runner, job, chat_ctx)

    assert "(no output)" in chat_ctx.messages[-1].content


@pytest.mark.asyncio
async def test_unknown_duration_when_timestamps_are_missing(runner, chat_ctx):
    job = _job(status="completed", finished_at=None)
    await monitor_job(runner, job, chat_ctx)

    assert "unknown" in chat_ctx.messages[-1].content


@pytest.mark.asyncio
async def test_running_job_polls_until_it_finishes(runner, chat_ctx, monkeypatch):
    """The 30s poll posts a tail snippet each time new output appears."""
    import includes.chat.job_progress as mod

    job = _job(status="running", output=["l1"])
    sleeps: list[float] = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            job.output.append("l2")
        else:
            job.status = "completed"

    monkeypatch.setattr(mod.asyncio, "sleep", _fake_sleep)
    await monitor_job(runner, job, chat_ctx)

    assert sleeps == [30, 30]
    progress = [m.content for m in chat_ctx.messages if "still running" in m.content]
    assert len(progress) == 1
    assert "l1\nl2" in progress[0]
    assert "**Completed**" in chat_ctx.messages[-1].content


@pytest.mark.asyncio
async def test_no_progress_message_when_output_has_not_grown(runner, chat_ctx, monkeypatch):
    import includes.chat.job_progress as mod

    job = _job(status="running", output=["l1"])
    calls = {"n": 0}

    async def _fake_sleep(seconds):
        calls["n"] += 1
        job.status = "completed"

    monkeypatch.setattr(mod.asyncio, "sleep", _fake_sleep)
    await monitor_job(runner, job, chat_ctx)

    assert not [m for m in chat_ctx.messages if "still running" in m.content]
