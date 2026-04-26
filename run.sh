#!/bin/bash

# Ensure local PostgreSQL is running
echo "🐘 Checking local PostgreSQL container..."
./start_postgres.sh

# Load environment variables
source .env 2>/dev/null || true

# Run FastAPI + Chainlit with reload mode
echo "🚀 Starting EagleAgent locally..."
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
