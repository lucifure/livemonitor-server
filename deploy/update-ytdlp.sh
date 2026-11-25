#!/usr/bin/env bash
# Upgrade yt-dlp in the LiveMonitor virtual environment, reapply the live-stall
# patch, then restart.
set -euo pipefail

PROJECT_DIR=/home/ubuntu/livemonitor-server
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Refusing to update: run deploy/setup.sh first." >&2
    exit 1
fi

"$VENV_PYTHON" -m pip install --upgrade "yt-dlp[default]"

YTDLP_VIDEO_FILE=$("$VENV_PYTHON" -c 'import yt_dlp.extractor.youtube._video as v; print(v.__file__)')
OLD_STALL_LIMIT_COUNT=$(grep -cF 'no_fragment_score > 30:' "$YTDLP_VIDEO_FILE" || true)
NEW_STALL_LIMIT_COUNT=$(grep -cF 'no_fragment_score > 600:' "$YTDLP_VIDEO_FILE" || true)

if [[ "$NEW_STALL_LIMIT_COUNT" == 1 ]]; then
    echo "yt-dlp live-stall patch already applied (limit 600)."
elif [[ "$OLD_STALL_LIMIT_COUNT" != 1 ]]; then
    echo "ERROR: expected exactly one 'no_fragment_score > 30:' line, found $OLD_STALL_LIMIT_COUNT. Not patching." >&2
    exit 2
else
    [[ -e "$YTDLP_VIDEO_FILE.orig" ]] || cp -p "$YTDLP_VIDEO_FILE" "$YTDLP_VIDEO_FILE.orig"
    sed -i 's/no_fragment_score > 30:/no_fragment_score > 600:/' "$YTDLP_VIDEO_FILE"
    if "$VENV_PYTHON" -m py_compile "$YTDLP_VIDEO_FILE" && [[ "$(grep -cF 'no_fragment_score > 600:' "$YTDLP_VIDEO_FILE" || true)" == 1 ]]; then
        echo "PATCHED: yt-dlp live-stall limit 30 -> 600. Backup: $YTDLP_VIDEO_FILE.orig"
    else
        cp -p "$YTDLP_VIDEO_FILE.orig" "$YTDLP_VIDEO_FILE"
        echo "ERROR: patched file failed to compile; original restored." >&2
        exit 3
    fi
fi

sudo systemctl restart livemonitor
sudo systemctl --no-pager --full status livemonitor
