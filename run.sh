#!/bin/bash

# Load environment variables
source .env 2>/dev/null || true

# Set default temp files folder if not provided
TEMP_FILES_FOLDER="${TEMP_FILES_FOLDER:-.files}"

uv run chainlit run app.py -w --files-upload-folder "$TEMP_FILES_FOLDER"
