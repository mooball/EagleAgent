#!/bin/bash

# Load environment variables
source .env 2>/dev/null || true

# Run Chainlit with watch mode
# TEMP_FILES_FOLDER is configured via environment variable (loaded from .env)
uv run chainlit run app.py -w
