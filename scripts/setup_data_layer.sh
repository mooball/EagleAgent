#!/bin/bash
# Quick setup script for Chainlit data layer with PostgreSQL in Docker

set -e

echo "🚀 Setting up Chainlit Data Layer with PostgreSQL..."
echo ""

# Check if Docker is running
if ! docker ps > /dev/null 2>&1; then
    echo "❌ Error: Docker is not running. Please start Docker and try again."
    exit 1
fi

# Stop and remove existing container if it exists
if docker ps -a | grep -q chainlit-postgres; then
    echo "🗑️  Removing existing chainlit-postgres container..."
    docker stop chainlit-postgres 2>/dev/null || true
    docker rm chainlit-postgres 2>/dev/null || true
fi

# Start PostgreSQL container
echo "🐘 Starting PostgreSQL container..."
docker run -d \
  --name chainlit-postgres \
  -e POSTGRES_PASSWORD=chainlit_secret \
  -e POSTGRES_DB=chainlit_db \
  -e POSTGRES_USER=chainlit \
  -p 5432:5432 \
  postgres:14

echo "⏳ Waiting for PostgreSQL to be ready..."
sleep 5

# Check if the database is ready
until docker exec chainlit-postgres pg_isready -U chainlit > /dev/null 2>&1; do
    echo "   Still waiting..."
    sleep 2
done

echo "✅ PostgreSQL is running!"
echo ""

# Clone and set up the official data layer schema
TEMP_DIR="/tmp/chainlit-datalayer-$$"
echo "📦 Cloning Chainlit data layer schema..."
git clone --depth 1 https://github.com/Chainlit/chainlit-datalayer.git "$TEMP_DIR"

cd "$TEMP_DIR"

# Create .env file
DATABASE_URL="postgresql://chainlit:chainlit_secret@localhost:5432/chainlit_db"
echo "DATABASE_URL=$DATABASE_URL" > .env

echo "📝 Installing dependencies..."
npm install --silent

echo "🔧 Running database migrations..."
npm run migrate

echo ""
echo "✅ Data layer setup complete!"
echo ""
echo "📋 Next steps:"
echo "1. Add this to your .env file:"
echo ""
echo "   DATABASE_URL=\"$DATABASE_URL\""
echo ""
echo "2. Restart your Chainlit app:"
echo "   ./run.sh"
echo ""
echo "3. Look for the History icon in the sidebar!"
echo ""
echo "💡 Useful commands:"
echo "   docker stop chainlit-postgres   # Stop the database"
echo "   docker start chainlit-postgres  # Start the database"
echo "   docker logs chainlit-postgres   # View logs"
echo ""

# Clean up
cd - > /dev/null
rm -rf "$TEMP_DIR"
