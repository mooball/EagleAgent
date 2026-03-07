#!/bin/bash
set -e

echo "🚀 Starting EagleAgent on Railway..."

# Validate required environment variables
required_vars=(
    "GOOGLE_API_KEY"
    "CHAINLIT_AUTH_SECRET"
    "OAUTH_GOOGLE_CLIENT_ID"
    "OAUTH_GOOGLE_CLIENT_SECRET"
    "DATABASE_URL"
)

for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ]; then
        echo "❌ Error: Required environment variable $var is not set"
        exit 1
    fi
done

echo "✅ All required environment variables are set"

# Ensure Data directory exists
DATA_DIR=${DATA_DIR:-/data}
mkdir -p "$DATA_DIR/attachments"
echo "✅ Ensured $DATA_DIR directory exists"

# Run Alembic Database Migrations
echo "🔧 Running database migrations..."
uv run alembic upgrade head
echo "✅ Database migrations complete"

# Set default port if not provided (Railway usually provides PORT)
PORT=${PORT:-8080}
echo "🌐 Starting Chainlit on port $PORT"

# Start Chainlit in production mode
exec uv run chainlit run app.py \
    --host 0.0.0.0 \
    --port $PORT