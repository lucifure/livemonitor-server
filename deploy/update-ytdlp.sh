#!/usr/bin/env bash
# Upgrade only yt-dlp in the LiveMonitor virtual environment, then restart.
set -euo pipefail

PROJECT_DIR=/home/ubuntu/livemonitor-server
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Refusing to update: run deploy/setup.sh first." >&2
    exit 1
fi

"$VENV_PYTHON" -m pip install --upgrade yt-dlp
sudo systemctl restart livemonitor
sudo systemctl --no-pager --full status livemonitor
