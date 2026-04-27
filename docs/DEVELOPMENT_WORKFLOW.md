# Development Workflow Guide

Quick reference for the recommended development workflow for EagleAgent.

## Daily Development Cycle

```bash
# Start local Postgres (if using docker-compose)
./start_postgres.sh

# Start development server (FastAPI + Chainlit)
./run.sh

# Edit files -> Save -> Auto-refresh in browser at http://localhost:8000
# Dashboard: http://localhost:8000/
# Chat UI:   http://localhost:8000/chat

# Stop the server
./kill-8000.sh
```

## Running Tests

```bash
# Run all tests (requires PostgreSQL running)
uv run pytest tests/ -x --timeout=60 -q --no-header

# Run a specific test file
uv run pytest tests/test_dashboard_routes.py -v

# Run with coverage
uv run pytest tests/ --cov=. --cov-report=html
```

## Before Committing Code

```bash
# Run full test suite
uv run pytest tests/ -x --timeout=60

# Check for import issues
python -c "from includes.agents import Supervisor; print('OK')"
```

## Database Schema Changes

If you modify SQLAlchemy models in `includes/dashboard/models.py`:
```bash
# Autogenerate an Alembic migration
uv run alembic revision --autogenerate -m "Describe your changes"

# Apply it locally
uv run alembic upgrade head
```

## Dashboard Development

- **Routes**: `includes/dashboard/routes.py` — add new pages or HTMX partials
- **Templates**: `templates/` — Jinja2 templates using `base.html` layout
- **Models**: `includes/dashboard/models.py` — SQLAlchemy ORM models
- **Styling**: Tailwind CSS via `public/tailwind.min.css`

## Deployment on Railway

Deployment is automated via Railway's GitHub integration. Push to `main` to trigger.

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
# Kill the process
./kill-8000.sh
```

### Can't reach the database
Verify `DATABASE_URL` is correct in `.env`. Ensure your PostgreSQL database is running:
```bash
./start_postgres.sh
```
