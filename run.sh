#!/bin/bash

# Ensure local PostgreSQL is running
echo "🐘 Checking local PostgreSQL container..."
./start_postgres.sh

# Load environment variables
source .env 2>/dev/null || true

# Run Chainlit with watch mode
echo "🚀 Starting Chainlit locally..."
uv run chainlit run app.py -w
