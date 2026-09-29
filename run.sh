#!/bin/bash

# Ensure local PostgreSQL is running
echo "🐘 Checking local PostgreSQL container..."
./start_postgres.sh

# Load environment variables
source .env 2>/dev/null || true

# Rebuild Tailwind CSS (ensures new utility classes are included)
echo "🎨 Rebuilding Tailwind CSS..."
if ! command -v tailwindcss >/dev/null 2>&1; then
    echo "❌ tailwindcss not found on PATH."
    echo "   Install the Tailwind v4 standalone CLI (see AGENTS.md → Frontend / UI)."
    exit 1
fi
tailwindcss -i input.css -o public/tailwind.min.css --minify

# Run the FastAPI app with reload mode. Override the port with PORT=<n> (e.g. to
# run a second instance from another worktree without colliding on 8000).
PORT="${PORT:-8000}"
echo "🚀 Starting EagleAgent locally on port ${PORT}..."
uv run uvicorn main:app --reload --host 0.0.0.0 --port "${PORT}"
