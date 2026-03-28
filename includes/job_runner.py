"""
Async background job runner for server-side script execution.

Spawns registered scripts as child processes via asyncio.create_subprocess_exec,
tracks their status in memory, captures output, and provides cancel/query APIs.

Usage:
    runner = JobRunner()
    await runner.start()          # starts reaper + signal handlers
    job = await runner.run_script("update_product_embeddings")
    status = runner.get_job(job.id)
    await runner.cancel(job.id)
    await runner.shutdown()       # kills children on app teardown
"""

import asyncio
import logging
import os
import signal
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config.scripts import get_script, validate_args

logger = logging.getLogger(__name__)

# Maximum lines of stdout/stderr kept per job
_OUTPUT_BUFFER_SIZE = 200


@dataclass
class Job:
    """In-memory representation of a running or finished job."""

    id: str
    script_name: str
    command: list[str]
    status: str  # running | completed | failed | cancelled
    started_at: datetime
    finished_at: Optional[datetime] = None
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    thread_id: Optional[str] = None  # Chainlit thread that started the job
    output: deque = field(default_factory=lambda: deque(maxlen=_OUTPUT_BUFFER_SIZE))

    # Internal — not serialised
    _process: Optional[asyncio.subprocess.Process] = field(
        default=None, repr=False, compare=False
    )


class JobRunner:
    """Manages background script execution."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._reaper_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background reaper that monitors running processes."""
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())
            logger.info("JobRunner reaper started")

    async def shutdown(self) -> None:
        """Kill all running children and stop the reaper."""
        for job in self._jobs.values():
            if job.status == "running" and job._process:
                try:
                    job._process.terminate()
                    logger.info(f"Terminated job {job.id} (pid {job.pid})")
                except ProcessLookupError:
                    pass
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
        logger.info("JobRunner shut down")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_script(
        self,
        script_name: str,
        args: list[str] | None = None,
        thread_id: str | None = None,
    ) -> Job:
        """Spawn a registered script as a child process.

        Raises:
            ValueError: if the script is unknown, args are invalid, or it's
                        already running.
        """
        script = get_script(script_name)
        if script is None:
            raise ValueError(f"Unknown script: {script_name}")

        # Guard against duplicate runs
        for existing in self._jobs.values():
            if existing.script_name == script_name and existing.status == "running":
                raise ValueError(
                    f"Script '{script_name}' is already running (job {existing.id})"
                )

        safe_args = validate_args(script_name, args or [])
        full_command = script["command"] + safe_args

        # Inherit the current environment so scripts get GOOGLE_API_KEY etc.
        proc = await asyncio.create_subprocess_exec(
            *full_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )

        job = Job(
            id=str(uuid.uuid4()),
            script_name=script_name,
            command=full_command,
            status="running",
            started_at=datetime.now(timezone.utc),
            pid=proc.pid,
            thread_id=thread_id,
            _process=proc,
        )
        self._jobs[job.id] = job
        logger.info(
            f"Started job {job.id}: {' '.join(full_command)} (pid {proc.pid})"
        )

        # Kick off a task to stream output lines into the buffer
        asyncio.create_task(self._stream_output(job))

        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        """Look up a job by ID."""
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        """Return all tracked jobs (running + recent finished)."""
        return list(self._jobs.values())

    async def cancel(self, job_id: str) -> Job:
        """Send SIGTERM to a running job.

        Raises ValueError if the job doesn't exist or isn't running.
        """
        job = self._jobs.get(job_id)
        if job is None:
            raise ValueError(f"Unknown job: {job_id}")
        if job.status != "running":
            raise ValueError(f"Job {job_id} is not running (status: {job.status})")

        if job._process:
            try:
                job._process.terminate()
            except ProcessLookupError:
                pass

        job.status = "cancelled"
        job.finished_at = datetime.now(timezone.utc)
        logger.info(f"Cancelled job {job.id} (pid {job.pid})")
        return job

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _stream_output(self, job: Job) -> None:
        """Read stdout line-by-line into the job's output buffer."""
        try:
            assert job._process and job._process.stdout
            async for raw_line in job._process.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                job.output.append(line)
        except Exception as e:
            logger.debug(f"Output stream ended for job {job.id}: {e}")

    async def _reaper_loop(self) -> None:
        """Periodically check running processes and update their status."""
        try:
            while True:
                for job in list(self._jobs.values()):
                    if job.status != "running" or job._process is None:
                        continue
                    ret = job._process.returncode
                    if ret is not None:
                        job.exit_code = ret
                        job.finished_at = datetime.now(timezone.utc)
                        if job.status == "running":  # not already cancelled
                            job.status = "completed" if ret == 0 else "failed"
                        if ret != 0:
                            job.error = f"Exit code {ret}"
                        logger.info(
                            f"Job {job.id} finished: {job.status} (exit {ret})"
                        )
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            return
