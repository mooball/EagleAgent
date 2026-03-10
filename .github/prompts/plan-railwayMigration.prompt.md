## Plan: Migrate to Railway, PostgreSQL, and Local File Storage

This migration will transition the EagleAgent project from Google Cloud Run, Firestore, SQLite, and GCS to Railway using PostgreSQL for all data storage and a local mounted volume for file storage.

**Steps**

**Phase 1: Cleanup & Teardown**
1. Delete GitHub Action deployments (`.github/workflows/deploy-cloud-run.yml`).
2. Delete GCP-specific deployment scripts (`start-cloudrun.sh`, `CLOUD_RUN_DEPLOYMENT.md`, `run_in_docker.sh`).
3. Delete Firebase/Firestore modules (`includes/firestore_store.py`, `includes/timestamped_firestore_saver.py`, `scripts/clear_checkpoints.py`, `scripts/force_delete_checkpoints.py`, `scripts/list_checkpoints.py`).
4. Delete GCS storage modules (`includes/gcs_storage_client.py`).
5. Remove GCP and SQLite dependencies from `pyproject.toml` (e.g., `google-cloud-firestore`, `google-cloud-storage`, `aiosqlite`) and add PostgreSQL dependencies (`asyncpg`, `psycopg2-binary`, `langgraph-checkpoint-postgres`, `alembic`).

**Phase 2: Reconfigure Storage & Databases**
1. Update `config/settings.py` and `.env` formats to use a standard `DATABASE_URL` (pointing to the production Railway PostgreSQL instance for both dev and prod).
2. **Short-term Memory & Data Layer**: Configure Chainlit Data Layer in `app.py` to use `postgresql+asyncpg://` instead of SQLite.
3. **Checkpoints**: Replace `TimestampedFirestoreSaver` with LangGraph's native `AsyncPostgresSaver`.
4. **Long-term Memory**: Replace `FirestoreStore` with LangGraph's `AsyncPostgresStore` (or a custom Postgres store) for agent memory.
5. **File Storage**: Rewrite `includes/storage_utils.py` to save and retrieve files to a local directory defined by a `DATA_DIR` environment variable (e.g., `/data` in production, `./data` locally) instead of GCS. 

**Phase 3: Schema Management (Alembic)**
1. Initialize an Alembic environment (`alembic init alembic`) to manage database migrations.
2. Define SQLAlchemy models for any custom schemas (like agent memory if not leveraging LangGraph's built-in store) so Alembic can autogenerate migrations.
3. Add an entrypoint script (or update the Dockerfile) to run `alembic upgrade head` on startup before launching Chainlit to ensure the DB is always up-to-date.

**Phase 4: Reconfigure Secrets & Environment**
1. Document the `.env` setup: The local `.env` will harbor the Railway Production `DATABASE_URL` and Google Auth credentials. Railway's dashboard will hold these variables in production.
2. Ensure `app.py` and `config/settings.py` correctly prioritize environment variables over default fallbacks for secrets.

**Phase 5: Reconfigure Dockerfile & Deployment**
1. Update `Dockerfile` to strip out GCP FUSE requirements.
2. Ensure `Dockerfile` mounts/creates the `/app/data` directory with appropriate permissions.
3. Set the Dockerfile `CMD` to start the app using `chainlit run app.py --host 0.0.0.0 --port $PORT` (potentially chained after the Alembic migration command).
4. Add Railway specific config (if necessary, though the Dockerfile is usually auto-detected). Ensure the Railway service is mapped with a Volume at `/app/data/`.

**Relevant files**
- `pyproject.toml` — Update dependencies (add asyncpg, alembic, langgraph-checkpoint-postgres).
- `app.py` — Update imports, Data Layer initialization, Checkpointer, and Store.
- `includes/storage_utils.py` — Refactor to use local `/data/` disk reads/writes.
- `config/settings.py` — Replace GCP configuration variables with purely DB/Local paths.
- `Dockerfile` — Simplify and clean up to a standard Python backend Dockerfile without GCS FUSE.
- `alembic.ini` and `alembic/` (New) — For DB schema migrations.

**Verification**
1. **Automated Tests**: Update the test suite (`test_checkpoint_saver.py`, `test_file_attachments.py`) to mock Postgres and Local disk instead of GCP/Firestore. Run the test suite.
2. **Local Run**: Launch the agent locally connected to the remote Railway PG database. Verify that session creation, user auth (Google), chatting, memory saving, and file attachments work properly. 
3. **Database Check**: Connect directly to the Railway PG database and ensure Alembic versions, Chainlit tables, and Checkpoint tables are populated.
4. **Railway Deployment**: Push the repository to Git to trigger a Railway build, bind the volume, and test the live application.

**Decisions**
- Dev uses Prod DB: No local DB (Postgres container) is needed. Developers simply need the Railway Postgres connection string.
- The `data` directory will be configurable via a `.env` variable (e.g., `DATA_DIR`), defaulting to `/data` in production where the Railway Volume is mounted, and `./data` for local development.
- We will rely on Alembic for custom tables, though LangGraph and Chainlit can handle their own internal table creations if instructed to.

**Further Considerations**
(None at this time. Plan is ready for execution.)
