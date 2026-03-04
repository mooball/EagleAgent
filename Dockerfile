# Multi-stage Dockerfile for EagleAgent with Python (uv) and Node.js for MCP servers
FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    gnupg \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20.x LTS for MCP servers (npx)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
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
COPY includes/ ./includes/
COPY config/ ./config/
COPY scripts/ ./scripts/

# Create directories
RUN mkdir -p /tmp/files /data

# Create non-root user for security
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app /tmp/files /data

# Switch to non-root user
USER appuser

# Expose port 8080 (Cloud Run standard)
EXPOSE 8080

# Copy and set startup script
COPY --chown=appuser:appuser start-cloudrun.sh ./
RUN chmod +x start-cloudrun.sh

# Run startup script
CMD ["./start-cloudrun.sh"]
