#!/bin/bash
set -e

echo "🚀 Starting EagleAgent on Cloud Run..."

# Validate required environment variables
required_vars=(
    "GOOGLE_API_KEY"
    "CHAINLIT_AUTH_SECRET"
    "OAUTH_GOOGLE_CLIENT_ID"
    "OAUTH_GOOGLE_CLIENT_SECRET"
)

for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ]; then
        echo "❌ Error: Required environment variable $var is not set"
        exit 1
    fi
done

echo "✅ All required environment variables are set"

# Create temp files directory for ephemeral uploads
mkdir -p /tmp/files
echo "✅ Created /tmp/files directory"

# Ensure /data directory exists (GCSFuse mount point)
mkdir -p /data
echo "✅ Ensured /data directory exists"

# Wait for GCSFuse mount to be ready
echo "⏳ Waiting for GCSFuse mount at /data..."
max_attempts=30
attempt=0

while [ $attempt -lt $max_attempts ]; do
    if touch /data/.write_test 2>/dev/null; then
        rm -f /data/.write_test
        echo "✅ GCSFuse mount at /data is ready and writable"
        break
    fi
    
    attempt=$((attempt + 1))
    if [ $attempt -eq $max_attempts ]; then
        echo "❌ Error: GCSFuse mount at /data is not writable after $max_attempts attempts"
        exit 1
    fi
    
    echo "   Attempt $attempt/$max_attempts - waiting for mount..."
    sleep 2
done

# Create database directory structure in GCS mount
mkdir -p /data/database
echo "✅ Created /data/database directory for SQLite"

# Initialize database schema if database doesn't exist or is empty
DB_PATH="/data/database/chainlit_datalayer.db"
if [ ! -f "$DB_PATH" ] || ! sqlite3 "$DB_PATH" "SELECT name FROM sqlite_master WHERE type='table' AND name='users';" 2>/dev/null | grep -q users; then
    echo "🔧 Initializing database schema..."
    cd /data/database
    uv run python /app/scripts/init_sqlite_db.py
    cd /app
    echo "✅ Database schema initialized"
else
    echo "✅ Database already initialized"
fi

# Set default port if not provided
PORT=${PORT:-8080}
echo "🌐 Starting Chainlit on port $PORT"

# Start Chainlit in production mode
# Note: File upload folder is configured via TEMP_FILES_FOLDER env var
exec uv run chainlit run app.py \
    --host 0.0.0.0 \
    --port $PORT
