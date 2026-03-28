# Server-Side Script Execution

EagleAgent allows admin users to run registered server-side scripts directly from the chat UI. Scripts run as background processes — the chat remains responsive while they execute.

## How It Works

1. **Admin asks** to run a task (e.g. "update the product embeddings").
2. The agent shows a **confirmation prompt** with Run / Cancel buttons.
3. On **Run**, the script starts as a child process. A start message with a Cancel button appears.
4. **Progress updates** are posted every 30 seconds while the script runs.
5. On completion, a **final message** shows duration and last output lines.

## Available Scripts

Scripts are defined in `config/scripts.py`. Current registry:

| Script | Description |
|--------|-------------|
| `update_product_embeddings` | Regenerate missing product vector embeddings (batches of 100) |
| `update_supplier_embeddings` | Regenerate missing supplier vector embeddings (batches of 100) |
| `import_products` | Import products from CSV files in the import directory |
| `import_suppliers` | Import suppliers from CSV (two-phase: upsert then brand linking) |
| `import_brands` | Import brands from CSV files in the import directory |
| `import_purchase_history` | Import purchase history from CSV files |

## Chat Commands

Admins can use natural language:

- **"update the product embeddings"** → triggers `run_script`
- **"what scripts are available?"** → triggers `list_scripts`
- **"what jobs are running?"** → triggers `list_jobs`
- **"check the status of the embedding job"** → triggers `get_job_status`
- **"cancel that job"** → triggers `cancel_job`

## Adding a New Script

1. Create your script in `scripts/` (e.g. `scripts/my_new_task.py`).
2. Add an entry to `SCRIPT_REGISTRY` in `config/scripts.py`:
   ```python
   "my_new_task": {
       "command": ["uv", "run", "python", "-m", "scripts.my_new_task"],
       "description": "Short description shown to admins",
       "args_allowed": [],      # flags that may be appended
       "long_running": True,    # if True, progress updates are sent
   },
   ```
3. That's it — the script is immediately available to admins in the chat.

## Architecture

### Key Modules

| Module | Purpose |
|--------|---------|
| `config/scripts.py` | Script registry — allowlist of runnable scripts, argument validation |
| `includes/job_runner.py` | `JobRunner` class — async subprocess management, reaper, signal handling |
| `includes/job_progress.py` | Chainlit progress messages — start, periodic updates, completion |
| `includes/tools/job_tools.py` | LangGraph tool wrappers — `run_script`, `list_scripts`, `list_jobs`, `get_job_status`, `cancel_job` |

### Security

- **Only registered scripts can be run** — the registry acts as an allowlist preventing arbitrary command injection.
- **Argument validation** — only flags listed in `args_allowed` are accepted.
- **Admin-only** — all job tools are in `ADMIN_ONLY_TOOLS` and filtered out for non-admin users.
- **Confirmation required** — `run_script` always shows a Run/Cancel prompt before starting.

### Process Management

- Scripts are spawned via `asyncio.create_subprocess_exec` — non-blocking, captures stdout/stderr.
- A **reaper task** polls child processes every 2 seconds, updating job status on exit.
- **SIGTERM/SIGINT handlers** ensure children are terminated on container shutdown (Railway deploys).
- A **200-line output buffer** (ring buffer) captures recent output per job.
- **Duplicate guard** — the same script cannot run twice concurrently.

## Railway / Production Notes

- Railway containers are persistent (not serverless) — child processes survive for the container's lifetime.
- **Deploy during a job**: Railway restarts the container → the running job dies. Use `list_jobs` to check before deploying.
- **Memory**: Embedding scripts use bounded memory (batch processing, 100 at a time). Should be fine within Railway container limits.
- Scripts always run against the **local database** — on Railway, that IS the production database. No `--production` flag needed.
