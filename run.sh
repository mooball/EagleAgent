#!/bin/bash

# Ensure local PostgreSQL is running
echo "🐘 Checking local PostgreSQL container..."
./start_postgres.sh

# Load environment variables
source .env 2>/dev/null || true

# Rebuild Tailwind CSS (ensures new utility classes are included)
echo "🎨 Rebuilding Tailwind CSS..."
./tailwindcss -i input.css -o public/tailwind.min.css --minify

# Run FastAPI + Chainlit with reload mode
echo "🚀 Starting EagleAgent locally..."
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
