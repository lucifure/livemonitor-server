#!/usr/bin/env bash
set -uo pipefail

LM_HOST="${LM_HOST:-ubuntu@100.87.137.48}"
LM_KEY="${LM_KEY:-$HOME/.ssh/oracle.key}"
SSH_OPTS=(-i "$LM_KEY" -o ConnectTimeout=15 -o ServerAliveInterval=20)
RESULT_FILE="$HOME/storage/shared/Documents/$(basename "$0")-RESULT.txt"
mkdir -p "$(dirname "$RESULT_FILE")"
exec > >(tee "$RESULT_FILE") 2>&1

ssh "${SSH_OPTS[@]}" "$LM_HOST" 'bash -s' <<'REMOTE'
set -uo pipefail
recordings=/home/ubuntu/recordings
busy_files="$(find "$recordings" -type f -mmin -5 -size +100k -print 2>/dev/null)"
if [[ -n "$busy_files" ]]; then
    printf 'BUSY\n%s\n' "$busy_files"
    exit 3
fi

echo "IDLE: no recording file is both newer than five minutes and larger than 100k"
if sudo systemctl restart livemonitor; then
    :
else
    result=$?
    echo "ERROR: sudo systemctl restart livemonitor failed (exit $result)"
    exit "$result"
fi

active=no
for _ in {1..30}; do
    if systemctl is-active --quiet livemonitor; then
        active=yes
        break
    fi
    sleep 1
done
echo "Service active state: $(systemctl is-active livemonitor 2>&1)"
sudo systemctl --no-pager --full status livemonitor
if [[ "$active" != yes ]]; then
    echo "ERROR: livemonitor did not become active within 30 seconds"
    exit 1
fi
REMOTE
