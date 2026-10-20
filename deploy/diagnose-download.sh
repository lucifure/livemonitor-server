#!/usr/bin/env bash
set -uo pipefail

PROXY_URL="http://127.0.0.1:1080"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_FILE="$PROJECT_DIR/config.json"
TEMP_DIR="/tmp/ytdiag"
REPORT_FILE="$HOME/ytdiag-report.txt"
USER_AGENT="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--wait" ) ]]; then
    echo "Usage: $0 [--wait]"
    exit 2
fi
WAIT_MODE=0
if [[ $# -eq 1 ]]; then WAIT_MODE=1; fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "config.json not found at $CONFIG_FILE"
    exit 1
fi

mapfile -t CHANNELS < <(python3 - "$CONFIG_FILE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as config_file:
    config = json.load(config_file)
for channel in config.get("channels", []):
    name = str(channel.get("name", "channel"))
    url = str(channel.get("url", "")).rstrip("/")
    if not url:
        continue
    if not url.endswith("/live"):
        url += "/live"
    print(name, url, channel.get("quality", "480p"), sep="\t")
PY
)

if [[ -f "$PROJECT_DIR/.venv/bin/yt-dlp" ]]; then
    YTDLP_BIN="$PROJECT_DIR/.venv/bin/yt-dlp"
elif command -v yt-dlp >/dev/null 2>&1; then
    YTDLP_BIN="$(command -v yt-dlp)"
else
    YTDLP_BIN="$HOME/.local/bin/yt-dlp"
fi

rm -rf "$TEMP_DIR"
mkdir -p "$TEMP_DIR"
trap 'rm -rf "$TEMP_DIR"' EXIT

echo "yt-dlp binary: $YTDLP_BIN"
echo "yt-dlp version: $("$YTDLP_BIN" --version 2>&1 || true)"
echo "ffmpeg: $(ffmpeg -version 2>&1 | head -n 1)"
if which ffprobe >/dev/null 2>&1; then
    echo "ffprobe: $(which ffprobe)"
else
    echo "ffprobe is missing"
fi

echo "Public IP direct: $(curl -s -m 15 https://ifconfig.me 2>/dev/null || true)"
echo "Public IP through proxy: $(curl -s -m 15 --proxy "$PROXY_URL" https://ifconfig.me 2>/dev/null || true)"

check_page() {
    local name="$1" url="$2" route="$3" body status size player consent
    body="$TEMP_DIR/page-${name}-${route}.html"
    if [[ "$route" == "proxy" ]]; then
        status="$(curl -s -L -m 15 --proxy "$PROXY_URL" -A "$USER_AGENT" -o "$body" -w '%{http_code}' "$url" 2>/dev/null || true)"
    else
        status="$(curl -s -L -m 15 -A "$USER_AGENT" -o "$body" -w '%{http_code}' "$url" 2>/dev/null || true)"
    fi
    size="$(wc -c < "$body" 2>/dev/null || echo 0)"
    player="no"
    consent="no"
    if grep -Fqi 'ytInitialPlayerResponse' "$body" 2>/dev/null; then player="yes"; fi
    if grep -Eqi 'consent\.youtube\.com|unusual traffic' "$body" 2>/dev/null; then consent="yes"; fi
    echo "Page $name ($route): HTTP ${status:-000}, ${size:-0} bytes, player data: $player, consent/bot check: $consent"
}

echo "YouTube /live page checks:"
for channel in "${CHANNELS[@]}"; do
    IFS=$'\t' read -r channel_name channel_url channel_quality <<< "$channel"
    check_page "$channel_name" "$channel_url" direct
    check_page "$channel_name" "$channel_url" proxy
done

is_live_now() {
    local url="$1" output last_line
    output="$(timeout -s INT 30 "$YTDLP_BIN" --simulate --no-warnings --print '%(is_live)s' "$url" 2>/dev/null || true)"
    last_line="$(printf '%s\n' "$output" | tail -n 1 | tr -d '\r')"
    [[ "$last_line" == "True" ]]
}

select_live_channel() {
    local channel name url quality
    for channel in "${CHANNELS[@]}"; do
        IFS=$'\t' read -r name url quality <<< "$channel"
        if is_live_now "$url"; then
            SELECTED_NAME="$name"
            SELECTED_URL="$url"
            SELECTED_QUALITY="$quality"
            return 0
        fi
    done
    return 1
}

format_selector() {
    case "${1,,}" in
        best) printf '%s' 'bestvideo+bestaudio/best' ;;
        360p) printf '%s' 'bestvideo[height<=360]+bestaudio/best[height<=360]/best' ;;
        720p) printf '%s' 'bestvideo[height<=720]+bestaudio/best[height<=720]/best' ;;
        1080p) printf '%s' 'bestvideo[height<=1080]+bestaudio/best[height<=1080]/best' ;;
        *) printf '%s' 'bestvideo[height<=480]+bestaudio/best[height<=480]/best' ;;
    esac
}

run_experiment() {
    local label="$1" mode="$2" live_from_start="$3" client="$4"
    local output_dir="$TEMP_DIR/$label" output_template="$TEMP_DIR/$label/%(id)s.%(ext)s"
    local log_file="$TEMP_DIR/$label.log" bytes real_data first_error exit_status
    mkdir -p "$output_dir"
    local -a yt_args=("$YTDLP_BIN" --force-ipv4 --no-update -f "$(format_selector "$SELECTED_QUALITY")"
        --hls-use-mpegts --hls-prefer-native --skip-unavailable-fragments --fragment-retries 3)
    if [[ "$live_from_start" == "yes" ]]; then yt_args+=(--live-from-start); fi
    yt_args+=(--retries 3 --extractor-retries 3 --file-access-retries 3 --retry-sleep 5 --socket-timeout 10
        --no-part --force-overwrites -o "$output_template")
    if [[ "$client" != "none" ]]; then
        yt_args+=(--extractor-args "youtube:player_client=$client")
    fi
    if [[ "$mode" == "proxy" ]]; then yt_args+=(--proxy "$PROXY_URL"); fi
    yt_args+=("$SELECTED_URL")

    if timeout -s INT 40 "${yt_args[@]}" >"$log_file" 2>&1; then
        exit_status=0
    else
        exit_status=$?
    fi
    bytes="$(find "$output_dir" -type f -printf '%s\n' 2>/dev/null | awk '{total += $1} END {print total+0}')"
    if (( bytes > 204800 )); then real_data="yes"; else real_data="no"; fi
    first_error="$(grep -i -m 1 'ERROR' "$log_file" 2>/dev/null | tr '\t|' '  ' || true)"
    if [[ -z "$first_error" ]]; then first_error="(none)"; fi
    printf '%s\t%s\t%s\t%s\n' "$label (exit $exit_status)" "$bytes" "$real_data" "$first_error" >> "$RESULTS_FILE"
}

run_experiments() {
    RESULTS_FILE="$TEMP_DIR/results.tsv"
    printf 'Experiment\tBytes downloaded\tOver 200 KB\tFirst ERROR line\n' > "$RESULTS_FILE"
    run_experiment '1-warp-live-from-start' proxy yes none
    run_experiment '2-warp-no-live-from-start' proxy no none
    run_experiment '3-direct-live-from-start' direct yes none
    run_experiment '4-direct-no-live-from-start' direct no none
    run_experiment '5-direct-mweb' direct no mweb
    run_experiment '6-direct-tv' direct no tv
    run_experiment '7-direct-android-vr' direct no android_vr
    {
        echo "Selected live channel: $SELECTED_NAME ($SELECTED_URL)"
        echo
        column -t -s $'\t' "$RESULTS_FILE" 2>/dev/null || cat "$RESULTS_FILE"
    } | tee "$REPORT_FILE"
    echo "Report saved to $REPORT_FILE"
}

if [[ $WAIT_MODE -eq 0 ]]; then
    if ! select_live_channel; then
        echo "NO CHANNEL IS LIVE"
        exit 3
    fi
    run_experiments
    exit 0
fi

WAIT_STARTED="$(date +%s)"
check_number=0
while (( $(date +%s) - WAIT_STARTED < 86400 )); do
    check_number=$((check_number + 1))
    CHECK_STARTED="$(date +%s)"
    echo "Live check $check_number: checking configured channels"
    if select_live_channel; then
        run_experiments
        exit 0
    fi
    CHECK_ELAPSED=$(( $(date +%s) - CHECK_STARTED ))
    SLEEP_FOR=$((180 - CHECK_ELAPSED))
    REMAINING=$((86400 - ($(date +%s) - WAIT_STARTED)))
    if (( SLEEP_FOR > REMAINING )); then SLEEP_FOR="$REMAINING"; fi
    if (( SLEEP_FOR > 0 )); then sleep "$SLEEP_FOR"; fi
done
echo "NO CHANNEL IS LIVE AFTER 24 HOURS"
exit 3
