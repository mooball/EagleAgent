"""
Chat progress updates for background jobs.

Sends Chainlit messages when a job starts, periodically while running,
and on completion or failure.  Attaches a Cancel action button.
"""

import asyncio
import logging

import chainlit as cl

from includes.job_runner import Job, JobRunner

logger = logging.getLogger(__name__)

# How often (seconds) to post progress updates for long-running jobs
_PROGRESS_INTERVAL = 30


async def monitor_job(runner: JobRunner, job: Job) -> None:
    """Background task that posts Chainlit messages about a job's lifecycle.

    Call via ``asyncio.create_task(monitor_job(runner, job))`` right after
    starting a job.  Messages go to the thread stored in ``job.thread_id``.
    """
    # --- Start message with Cancel button ---
    cancel_action = cl.Action(
        name="cancel_job",
        payload={"job_id": job.id},
        label="Cancel",
        description=f"Cancel {job.script_name}",
    )

    start_msg = cl.Message(
        content=(
            f"**Started** `{job.script_name}` — job `{job.id[:8]}`, pid {job.pid}"
        ),
        actions=[cancel_action],
    )
    await start_msg.send()

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
            await cl.Message(
                content=f"**`{job.script_name}`** still running…\n```\n{snippet}\n```",
            ).send()
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
        await cl.Message(
            content=(
                f"**Completed** `{job.script_name}` in {duration}.\n"
                f"```\n{snippet}\n```"
            ),
        ).send()
    elif job.status == "failed":
        tail = list(job.output)[-5:]
        snippet = "\n".join(tail) if tail else "(no output)"
        await cl.Message(
            content=(
                f"**Failed** `{job.script_name}` (exit code {job.exit_code}) "
                f"after {duration}.\n```\n{snippet}\n```"
            ),
        ).send()
    elif job.status == "cancelled":
        await cl.Message(
            content=f"**Cancelled** `{job.script_name}` after {duration}.",
        ).send()
