#!/usr/bin/env bash
# Raise yt-dlp's live-stall limit on the VM so one recording survives a pause
# in the live stream instead of ending early.
#
# yt-dlp (live-from-start) asks YouTube every 5 s for the newest fragment. Each
# round without a new one adds 5 to a score and it gives up silently above 30
# (about 35 s). This changes that number to 600 (about 10 minutes of waiting).
# A stream that really ended is still detected within seconds (separate check).
#
# Usage (from Termux, in ~/livemonitor-server):
#   bash deploy/vm-patch-ytdlp-stall.sh            apply (safe to repeat)
#   bash deploy/vm-patch-ytdlp-stall.sh status     show whether it is applied
#   bash deploy/vm-patch-ytdlp-stall.sh revert     restore the original file
# Re-run "apply" after every yt-dlp upgrade; an upgrade replaces the file.
# It never restarts the service and never touches recordings; a running
# yt-dlp keeps the code it already loaded, the next recording uses the patch.
set -uo pipefail

LM_HOST="${LM_HOST:-ubuntu@100.87.137.48}"
LM_KEY="${LM_KEY:-$HOME/.ssh/oracle.key}"
SSH_OPTS=(-i "$LM_KEY" -o ConnectTimeout=15 -o ServerAliveInterval=20)
MODE="${1:-apply}"
RESULT_FILE="$HOME/storage/shared/Documents/$(basename "$0")-RESULT.txt"
mkdir -p "$(dirname "$RESULT_FILE")" 2>/dev/null
exec > >(tee "$RESULT_FILE") 2>&1

ssh "${SSH_OPTS[@]}" "$LM_HOST" bash -s -- "$MODE" "${LM_PY:-/home/ubuntu/livemonitor-server/.venv/bin/python}" <<'REMOTE'
set -uo pipefail
mode="$1"; py="$2"; limit=600
f="$("$py" -c 'import yt_dlp.extractor.youtube._video as v; print(v.__file__)')" || { echo "ERROR: cannot find yt-dlp"; exit 1; }
echo "yt-dlp $("$py" -c 'import yt_dlp.version as v; print(v.__version__)')"
echo "file: $f"
old=$(grep -c 'no_fragment_score > 30:' "$f")
new=$(grep -c "no_fragment_score > $limit:" "$f")
case "$mode" in
status)
    if [ "$new" = 1 ]; then echo "STATUS: patched (limit $limit)"
    elif [ "$old" = 1 ]; then echo "STATUS: not patched (original limit 30)"
    else echo "STATUS: unknown (code looks different; do not patch)"; exit 2; fi ;;
apply)
    if [ "$new" = 1 ]; then echo "Already patched (limit $limit). Nothing changed."; exit 0; fi
    if [ "$old" != 1 ]; then echo "ERROR: expected exactly one 'no_fragment_score > 30:' line, found $old. Not patching."; exit 2; fi
    [ -e "$f.orig" ] || cp -p "$f" "$f.orig"
    sed -i "s/no_fragment_score > 30:/no_fragment_score > $limit:/" "$f"
    if "$py" -m py_compile "$f" && [ "$(grep -c "no_fragment_score > $limit:" "$f")" = 1 ]; then
        echo "PATCHED: limit 30 -> $limit. Backup: $f.orig"
    else
        cp -p "$f.orig" "$f"; echo "ERROR: patched file failed to compile; original restored."; exit 3
    fi ;;
revert)
    if [ -e "$f.orig" ]; then cp -p "$f.orig" "$f" && "$py" -m py_compile "$f" && echo "REVERTED to the original file."
    else echo "No backup found; nothing to revert."; exit 2; fi ;;
*) echo "Unknown mode '$mode' (use apply, status or revert)"; exit 2 ;;
esac
REMOTE
