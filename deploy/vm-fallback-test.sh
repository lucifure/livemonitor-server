#!/usr/bin/env bash
set -uo pipefail

LM_HOST="${LM_HOST:-ubuntu@100.87.137.48}"
LM_KEY="${LM_KEY:-$HOME/.ssh/oracle.key}"
SSH_OPTS=(-i "$LM_KEY" -o ConnectTimeout=15 -o ServerAliveInterval=20)
RESULT_FILE="$HOME/storage/shared/Documents/$(basename "$0")-RESULT.txt"
mkdir -p "$(dirname "$RESULT_FILE")"
exec > >(tee "$RESULT_FILE") 2>&1

if [[ $# -ne 1 || ! "$1" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "Usage: $0 <channel handle, e.g. darth_mb>"
    exit 2
fi
CHANNEL_HANDLE=$1
if [[ "${LM_NO_PROXY:-}" == "1" ]]; then
    NO_PROXY=1
    echo "MODE: direct (no proxy)"
else
    NO_PROXY=0
    echo "MODE: via proxy"
fi

ssh "${SSH_OPTS[@]}" "$LM_HOST" "bash -s -- '$CHANNEL_HANDLE' '$NO_PROXY'" <<'REMOTE'
set -uo pipefail
handle=$1
no_proxy=$2
if [[ ! "$no_proxy" =~ ^[01]$ ]]; then
    echo "ERROR: proxy mode must be 0 or 1"
    exit 2
fi
url="https://youtube.com/@${handle}/live"
YTDLP=/home/ubuntu/.local/bin/yt-dlp
if [[ ! -x "$YTDLP" ]]; then
    echo "ERROR: yt-dlp not found at $YTDLP"
    exit 1
fi
echo "yt-dlp version: $($YTDLP --version)"
tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT

run_check() {
    local label=$1
    shift
    local result
    : > "$tmp_file"
    if [[ "$no_proxy" == "1" ]]; then
        if timeout 40 "$YTDLP" --simulate --print '%(is_live)s %(id)s' --no-warnings \
            --force-ipv4 "$@" "$url" >"$tmp_file" 2>&1; then
            result=0
        else
            result=$?
        fi
    elif timeout 40 "$YTDLP" --simulate --print '%(is_live)s %(id)s' --no-warnings \
        --force-ipv4 --proxy http://127.0.0.1:1080 "$@" "$url" >"$tmp_file" 2>&1; then
        result=0
    else
        result=$?
    fi
    echo "$label:"
    tail -n 3 "$tmp_file"
    echo "exit code: $result"
}

run_check "Without cookies"
cookie_file=/home/ubuntu/livemonitor-server/cookies.txt
if [[ -f "$cookie_file" ]]; then
    run_check "With cookies" --cookies "$cookie_file"
else
    echo "With cookies: skipped (cookies.txt is missing)"
fi
REMOTE
