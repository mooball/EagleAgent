#!/bin/bash
set -e

echo "🚀 Starting local PostgreSQL database..."
docker compose up -d postgres

echo "⏳ Waiting for PostgreSQL to be ready..."
sleep 5 # Give it a moment to boot

echo "✅ PostgreSQL is running on localhost:5432"
echo "🗄️   Database: eagleagent"
echo "👤 User: postgres"

echo "🔧 Running database migrations..."
if [ -f "alembic.ini" ]; then
    uv run alembic upgrade head
    echo "✅ Database migrations complete"
else
    echo "⚠️  alembic.ini not found. Skipping migrations."
fi
