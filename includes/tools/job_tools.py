"""
LangGraph tool wrappers for server-side script execution.

Provides admin-only tools that let the agent run registered scripts,
list running/completed jobs, check job status, and cancel jobs.
All tools delegate to the JobRunner instance.
"""

import chainlit as cl
from langchain_core.tools import tool

from config.scripts import list_scripts as _list_scripts
from includes.job_runner import JobRunner


def create_job_tools(runner: JobRunner):
    """Create job-management LangGraph tools bound to a JobRunner instance.

    All four tools are admin-only — add their names to ADMIN_ONLY_TOOLS
    so GeneralAgent strips them for non-admin users.

    Returns:
        [run_script, list_jobs, get_job_status, cancel_job]
    """

    @tool
    async def run_script(script_name: str) -> str:
        """Request to run a registered server-side script.

        This does NOT start the script immediately — it sends a confirmation
        message with Run / Cancel buttons so the admin can approve first.

        Call this when an admin asks to run a script, e.g. "update the product
        embeddings", "import the products", "run the supplier embeddings
        update".

        Available scripts can be listed with the list_scripts tool or by asking
        "what scripts are available?".

        Args:
            script_name: Name of the registered script (e.g. "update_product_embeddings").

        Returns:
            A message indicating that confirmation was requested.
        """
        from config.scripts import get_script

        script = get_script(script_name)
        if script is None:
            return f"Error: Unknown script '{script_name}'. Use `list_scripts` to see available scripts."

        confirm_action = cl.Action(
            name="confirm_run_script",
            payload={"script_name": script_name},
            label="Run",
            description=f"Start {script_name}",
        )
        cancel_action = cl.Action(
            name="cancel_run_script",
            payload={"script_name": script_name},
            label="Cancel",
            description="Cancel this script run",
        )

        await cl.Message(
            content=(
                f"**Ready to run** `{script_name}`\n\n"
                f"_{script['description']}_\n\n"
                f"Click **Run** to start or **Cancel** to abort."
            ),
            actions=[confirm_action, cancel_action],
            author="EagleAgent",
        ).send()

        return (
            f"Confirmation requested for `{script_name}`. "
            f"The user must click Run or Cancel before the script starts. "
            f"Once started, use list_jobs or get_job_status(script_name='{script_name}') "
            f"to check on it."
        )

    @tool
    async def list_scripts() -> str:
        """List all registered scripts that can be run from the chat.

        Call this when an admin asks "what scripts are available?",
        "what can I run?", or "show me the scripts".

        Returns:
            A formatted list of scripts with descriptions.
        """
        registry = _list_scripts()
        if not registry:
            return "No scripts are registered."

        lines = []
        for name, info in registry.items():
            lines.append(f"- **{name}**: {info['description']}")
        return "\n".join(lines)

    @tool
    async def list_jobs() -> str:
        """List all tracked jobs (running + recent completed/failed).

        Call this tool to discover job IDs and check on jobs. This is the
        first tool to use when asked about job status, especially after
        a script was started via a confirmation button.

        Call this when an admin asks "what jobs are running?",
        "show me the job list", "any active scripts?", "check the
        status", or "how is the job going?".

        Returns:
            A formatted table of jobs with status, runtime, and job IDs.
        """
        jobs = runner.list_jobs()
        if not jobs:
            return "No jobs have been run yet."

        lines = ["| Script | Status | Started | Duration | Job ID |",
                 "|--------|--------|---------|----------|--------|"]
        for j in jobs:
            started = j.started_at.strftime("%H:%M:%S")
            if j.finished_at:
                delta = j.finished_at - j.started_at
                duration = str(delta).split(".")[0]  # drop microseconds
            elif j.status == "running":
                from datetime import datetime, timezone
                delta = datetime.now(timezone.utc) - j.started_at
                duration = str(delta).split(".")[0] + " (running)"
            else:
                duration = "—"
            lines.append(
                f"| {j.script_name} | {j.status} | {started} | {duration} | `{j.id[:8]}` |"
            )
        return "\n".join(lines)

    @tool
    async def get_job_status(job_id: str = "", script_name: str = "") -> str:
        """Get detailed status and recent output for a specific job.

        You can look up the job by job_id OR by script_name. If you don't
        know the job ID, pass the script_name instead (e.g.
        "update_supplier_embeddings"). If you don't know either, use
        list_jobs first to see all jobs.

        Args:
            job_id: Full or partial (first 8 chars) job ID. Optional if script_name is given.
            script_name: Name of the script to look up. Optional if job_id is given.

        Returns:
            Job status, runtime info, and the last few lines of output.
        """
        job = None

        # Try by job_id first
        if job_id:
            job = runner.get_job(job_id)
            if job is None:
                # Try partial match
                for j in runner.list_jobs():
                    if j.id.startswith(job_id):
                        job = j
                        break

        # Fall back to script_name match (most recent match)
        if job is None and script_name:
            for j in reversed(runner.list_jobs()):
                if j.script_name == script_name:
                    job = j
                    break

        if job is None:
            if not job_id and not script_name:
                return "Please provide a job_id or script_name. Use list_jobs to see all jobs."
            return f"No job found matching job_id=`{job_id}` script_name=`{script_name}`."

        lines = [
            f"**Script**: {job.script_name}",
            f"**Status**: {job.status}",
            f"**PID**: {job.pid}",
            f"**Started**: {job.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        ]
        if job.finished_at:
            delta = job.finished_at - job.started_at
            lines.append(f"**Finished**: {job.finished_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
            lines.append(f"**Duration**: {str(delta).split('.')[0]}")
        if job.exit_code is not None:
            lines.append(f"**Exit code**: {job.exit_code}")
        if job.error:
            lines.append(f"**Error**: {job.error}")

        # Last 10 lines of output
        if job.output:
            tail = list(job.output)[-10:]
            lines.append("\n**Recent output:**")
            lines.append("```")
            lines.extend(tail)
            lines.append("```")

        return "\n".join(lines)

    @tool
    async def cancel_job(job_id: str) -> str:
        """Cancel a running job by sending SIGTERM.

        Args:
            job_id: Full or partial (first 8 chars) job ID.

        Returns:
            Confirmation that the job was cancelled.
        """
        # Support partial IDs
        target_id = job_id
        if runner.get_job(job_id) is None:
            for j in runner.list_jobs():
                if j.id.startswith(job_id):
                    target_id = j.id
                    break

        try:
            job = await runner.cancel(target_id)
            return f"Cancelled job `{job.id[:8]}` ({job.script_name})."
        except ValueError as e:
            return f"Error: {e}"

    return [run_script, list_scripts, list_jobs, get_job_status, cancel_job]
