#!/bin/bash
# Stop and remove the local Docker container

CONTAINER_NAME="eagleagent-container"

echo "🛑 Stopping EagleAgent Docker container..."

if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    docker stop $CONTAINER_NAME 2>/dev/null || true
    docker rm $CONTAINER_NAME 2>/dev/null || true
    echo "✅ Container stopped and removed"
else
    echo "ℹ️  No container named '$CONTAINER_NAME' found"
fi
