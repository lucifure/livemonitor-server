#!/usr/bin/env bash
# Install LiveMonitor on a fresh Ubuntu LTS VM. Run this from the checked-out repo.
set -euo pipefail

PROJECT_DIR=/home/ubuntu/livemonitor-server
VENV_DIR="$PROJECT_DIR/.venv"
SERVICE_NAME=livemonitor.service

if [[ ! -f "$PROJECT_DIR/config.json" ]]; then
    echo "Refusing to install: $PROJECT_DIR/config.json is missing." >&2
    echo "Copy config.example.json to config.json and fill in your own values first." >&2
    exit 1
fi

if [[ ! -f "$PROJECT_DIR/deploy/livemonitor.service" ]]; then
    echo "Refusing to install: run this from a complete checkout at $PROJECT_DIR." >&2
    exit 1
fi

if [[ ! -x /home/ubuntu/.deno/bin/deno ]]; then
    echo "Warning: Deno was not found at /home/ubuntu/.deno/bin/deno."
    echo "LiveMonitor can still start, but features that need Deno may not work."
fi

echo "Installing Ubuntu packages..."
sudo apt-get update
sudo apt-get install --yes python3-venv ffmpeg git

if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

echo "Installing pinned Python packages..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"

echo "Installing systemd service..."
sudo install -m 0644 "$PROJECT_DIR/deploy/livemonitor.service" "/etc/systemd/system/$SERVICE_NAME"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

echo
echo "LiveMonitor is installed and started."
echo "Next steps:"
echo "  1. Check it: sudo systemctl status livemonitor --no-pager"
echo "  2. Read logs: journalctl -u livemonitor -f"
echo "  3. Check /status with the bearer token from config.json."
