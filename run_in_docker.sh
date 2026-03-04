#!/bin/bash
set -e

echo "🐳 Building and running EagleAgent in Docker locally..."
echo ""

# Configuration
IMAGE_NAME="eagleagent-local"
CONTAINER_NAME="eagleagent-container"
HOST_PORT=8001
CONTAINER_PORT=8080

# Check if .env file exists
if [ ! -f .env ]; then
    echo "❌ Error: .env file not found"
    echo "   Please create .env file with required environment variables"
    echo "   You can copy from .env.example"
    exit 1
fi

# Stop and remove existing container if running
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "🛑 Stopping existing container..."
    docker stop $CONTAINER_NAME 2>/dev/null || true
    docker rm $CONTAINER_NAME 2>/dev/null || true
    echo "✅ Cleaned up existing container"
    echo ""
fi

# Build Docker image
echo "🔨 Building Docker image..."
docker build -t $IMAGE_NAME .
echo "✅ Docker image built: $IMAGE_NAME"
echo ""

# Run container
echo "🚀 Starting container..."
echo "   Container: $CONTAINER_NAME"
echo "   Local URL: http://localhost:$HOST_PORT"
echo "   Port mapping: $HOST_PORT -> $CONTAINER_PORT"
echo ""

# Run with environment variables from .env file
# Note: In local Docker, we don't mount GCS volumes, so files will be ephemeral
docker run -d \
    --name $CONTAINER_NAME \
    --env-file .env \
    -e DATABASE_URL="sqlite+aiosqlite:///./chainlit_datalayer.db" \
    -e TEMP_FILES_FOLDER="/tmp/files" \
    -e CHAINLIT_URL="http://localhost:$HOST_PORT" \
    -p $HOST_PORT:$CONTAINER_PORT \
    $IMAGE_NAME

echo "✅ Container started!"
echo ""
echo "📋 Useful commands:"
echo "   View logs:        docker logs -f $CONTAINER_NAME"
echo "   Stop container:   docker stop $CONTAINER_NAME"
echo "   Remove container: docker rm $CONTAINER_NAME"
echo "   Shell access:     docker exec -it $CONTAINER_NAME /bin/bash"
echo ""
echo "⏳ Waiting for application to start..."
sleep 3

# Show initial logs
echo ""
echo "📜 Recent logs:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
docker logs --tail 20 $CONTAINER_NAME
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Check if container is still running
if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "✅ Container is running!"
    echo "🌐 Open http://localhost:$HOST_PORT in your browser"
    echo ""
    echo "💡 To follow logs in real-time, run:"
    echo "   docker logs -f $CONTAINER_NAME"
else
    echo "❌ Container failed to start. Check logs above for errors."
    exit 1
fi
