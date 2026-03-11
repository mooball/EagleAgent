# Development Workflow Guide

Quick reference for the recommended development workflow for EagleAgent.

## Daily Development Cycle

```bash
# Start local Postgres (if using docker-compose)
./start_postgres.sh

# Start development server
./run.sh

# Edit files -> Save -> Auto-refresh in browser at http://localhost:8000

# Stop the server
./kill.sh
```

## Before Committing Code

```bash
# Run test suite
uv run pytest
```

## Database Schema Changes

If you make modifications to how the memory or state should be modeled in the database:
```bash
# Autogenerate an Alembic migration
uv run alembic revision -m "Describe your changes"

# Apply it locally
uv run alembic upgrade head
```

## Deployment on Railway

Deployment is automated via Railway's GitHub integration. When code is pushed to the `railway` branch (or your primary branch linked to Railway), Railway will automatically detect the `Dockerfile` and execute the build.

```bash
git add .
git commit -m "Update feature X"
git push origin main  # Triggers Railway deployment
```

Ensure your Railway project variables reflect the correct `DATABASE_URL` format.

## Troubleshooting

### Local server won't start
```bash
# Check if port is already in use
lsof -i :8000
kill <PID>
```

### Can't reach the database
Verify `DATABASE_URL` is correct in `.env`. Ensure your PostgreSQL database is running.
