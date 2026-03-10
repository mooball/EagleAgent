#!/bin/bash

echo "🛑 Stopping local PostgreSQL database..."
docker compose stop postgres

echo "✅ PostgreSQL database stopped."
echo "💡 (Note: Use 'docker compose down -v' if you want to wipe the local database completely)"
