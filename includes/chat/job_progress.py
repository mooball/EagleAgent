"""
Chat progress updates for background jobs.

Sends messages when a job starts, periodically while running,
and on completion or failure.  Attaches a Cancel action button.
"""

import asyncio
import logging

from includes.chat.context import ActionSpec, ChatContext
from includes.job_runner import Job, JobRunner

logger = logging.getLogger(__name__)

# How often (seconds) to post progress updates for long-running jobs
_PROGRESS_INTERVAL = 30


async def monitor_job(runner: JobRunner, job: Job, ctx: ChatContext) -> None:
    """Background task that posts messages about a job's lifecycle.

    Call via ``asyncio.create_task(monitor_job(runner, job, ctx))`` right after
    starting a job.
    """
    # --- Start message with Cancel button ---
    cancel_action = ActionSpec(
        name="cancel_job",
        payload={"job_id": job.id},
        label="Cancel",
        tooltip=f"Cancel {job.script_name}",
    )

    await ctx.say(
        f"**Started** `{job.script_name}` — job `{job.id[:8]}`, pid {job.pid}",
        actions=[cancel_action],
    )

    # --- Periodic progress ---
    last_output_len = 0
    while job.status == "running":
        await asyncio.sleep(_PROGRESS_INTERVAL)

        # Job may have finished while we slept
        if job.status != "running":
            break

        current_len = len(job.output)
        if current_len > last_output_len:
            tail = list(job.output)[-5:]
            snippet = "\n".join(tail)
            await ctx.say(f"**`{job.script_name}`** still running…\n```\n{snippet}\n```")
            last_output_len = current_len

    # --- Completion / Failure message ---
    if job.finished_at and job.started_at:
        delta = job.finished_at - job.started_at
        duration = str(delta).split(".")[0]
    else:
        duration = "unknown"

    if job.status == "completed":
        tail = list(job.output)[-3:]
        snippet = "\n".join(tail) if tail else "(no output)"
        await ctx.say(
            f"**Completed** `{job.script_name}` in {duration}.\n"
            f"```\n{snippet}\n```"
        )
    elif job.status == "failed":
        tail = list(job.output)[-5:]
        snippet = "\n".join(tail) if tail else "(no output)"
        await ctx.say(
            f"**Failed** `{job.script_name}` (exit code {job.exit_code}) "
            f"after {duration}.\n```\n{snippet}\n```"
        )
    elif job.status == "cancelled":
        await ctx.say(f"**Cancelled** `{job.script_name}` after {duration}.")
