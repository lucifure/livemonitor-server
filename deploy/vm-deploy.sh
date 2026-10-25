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
~/deploy-livemonitor.sh
deploy_result=$?
echo "Latest server commit:"
git -C /home/ubuntu/livemonitor-server log -1 --oneline
echo "Service active state:"
systemctl is-active livemonitor
active_result=$?
if (( deploy_result != 0 )); then exit "$deploy_result"; fi
exit "$active_result"
REMOTE
