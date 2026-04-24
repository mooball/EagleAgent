#!/bin/bash
set -e

# Ensure data directories are writable by the eagleagent user (uid 1000).
# When a Docker volume is mounted at /app/data it may be owned by root,
# preventing the non-root eagleagent user from creating subdirectories.
DATA_DIR=${DATA_DIR:-/app/data}
mkdir -p "$DATA_DIR/attachments" "$DATA_DIR/browser_downloads"
chown -R eagleagent:eagleagent "$DATA_DIR"

# Drop privileges and run the main start script
exec gosu eagleagent ./start.sh
