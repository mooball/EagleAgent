#!/bin/bash
set -e

echo "🚀 Starting EagleAgent on Railway..."

# Decode service account credentials from base64 env var (Railway deployment)
if [ -n "$GOOGLE_SERVICE_ACCOUNT_BASE64" ]; then
    echo "$GOOGLE_SERVICE_ACCOUNT_BASE64" | base64 -d > /tmp/service-account-key.json
    export GOOGLE_APPLICATION_CREDENTIALS="/tmp/service-account-key.json"
    echo "✅ Service account credentials decoded"
fi

# Validate required environment variables
required_vars=(
    "GOOGLE_GENAI_USE_VERTEXAI"
    "GOOGLE_CLOUD_PROJECT"
    "GOOGLE_APPLICATION_CREDENTIALS"
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
DATA_DIR=${DATA_DIR:-/app/data}
mkdir -p "$DATA_DIR/attachments"
echo "✅ Ensured $DATA_DIR directory exists"

# Run Alembic Database Migrations
echo "🔧 Running database migrations..."
uv run alembic upgrade head
echo "✅ Database migrations complete"

# Set default port if not provided (Railway usually provides PORT)
PORT=${PORT:-8080}
echo "🌐 Starting EagleAgent on port $PORT"

# Start FastAPI app (Chainlit is mounted at /chat)
# --proxy-headers: trust X-Forwarded-Proto from the reverse proxy so that
#   request.base_url reflects https:// (needed for OAuth redirect_uri).
exec uv run uvicorn main:app \
    --host 0.0.0.0 \
    --port $PORT \
    --proxy-headers \
    --forwarded-allow-ips='*'