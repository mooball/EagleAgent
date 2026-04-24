# Multi-stage Dockerfile for EagleAgent with Python (uv) and Node.js for MCP servers
FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    gnupg \
    sqlite3 \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20.x LTS for MCP servers (npx) and agent-browser
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    npm install -g agent-browser@0.16.3 && \
    npx -y playwright install-deps && \
    agent-browser install && \
    rm -rf /var/lib/apt/lists/*

# Install uv package manager
RUN pip install --no-cache-dir uv

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Sync dependencies (no dev dependencies)
RUN uv sync --frozen --no-dev

# Copy application code
COPY app.py ./
COPY chainlit.md ./
COPY .chainlit/ ./.chainlit/
COPY includes/ ./includes/
COPY config/ ./config/
COPY scripts/ ./scripts/
COPY public/ ./public/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Create directories
RUN mkdir -p /tmp/files /app/data/attachments /app/data/browser_downloads

# Create non-root user for security
RUN useradd -m -u 1000 eagleagent && \
    chown -R eagleagent:eagleagent /app /tmp/files

# Expose port 8080 (Railway default)
EXPOSE 8080

# Health check for orchestrators (Railway, Kubernetes, etc.)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8080/ || exit 1

# Copy and set startup scripts
COPY start.sh entrypoint.sh ./
RUN chmod +x start.sh entrypoint.sh

# Entrypoint runs as root to fix volume permissions, then drops to eagleagent
ENTRYPOINT ["./entrypoint.sh"]
