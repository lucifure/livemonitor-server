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

echo '===== 1. UTC date and warp container status ====='
date -u
docker ps -a --filter 'name=^/warp$' --format 'table {{.Names}}\t{{.Status}}' || echo 'docker ps failed'

echo '===== 2. Last 60 lines of warp container logs ====='
docker logs --tail 60 warp 2>&1 || echo 'docker logs failed'

echo '===== 3. Proxy value from config.json ====='
proxy="$(python3 -c 'import json; print(json.load(open("/home/ubuntu/livemonitor-server/config.json", encoding="utf-8")).get("proxy", ""))')"
proxy_status=$?
if [[ $proxy_status -ne 0 ]]; then
    echo "Could not read proxy config key (python exit $proxy_status)"
    proxy=""
else
    printf '%s\n' "$proxy"
fi

curl_trace() {
    local label="$1" use_proxy="$2" output status
    echo "===== $label ====="
    if [[ "$use_proxy" == yes ]]; then
        output="$(curl -sS -m 15 -x "$proxy" -w '\nHTTP %{http_code}\n' https://www.cloudflare.com/cdn-cgi/trace)"
    else
        output="$(curl -sS -m 15 -w '\nHTTP %{http_code}\n' https://www.cloudflare.com/cdn-cgi/trace)"
    fi
    status=$?
    printf '%s\n' "$output" | awk '/^HTTP / { http = $0; next } { if (body_lines < 6) { print; body_lines++ } } END { if (body_lines == 0) print "(no response body)"; if (http != "") print http }'
    if [[ $status -ne 0 ]]; then echo "curl exit code: $status"; fi
}

curl_youtube() {
    local label="$1" use_proxy="$2" status
    echo "===== $label ====="
    if [[ "$use_proxy" == yes ]]; then
        curl -sS -m 15 -x "$proxy" https://www.youtube.com/ -o /dev/null -w '%{http_code}\n'
    else
        curl -sS -m 15 https://www.youtube.com/ -o /dev/null -w '%{http_code}\n'
    fi
    status=$?
    if [[ $status -ne 0 ]]; then echo "curl exit code: $status"; fi
}

curl_trace '4. Cloudflare trace through configured proxy (HTTP result and first six lines)' yes
curl_youtube '5. YouTube through configured proxy (HTTP result)' yes
curl_trace '6a. Cloudflare trace without proxy (HTTP result and first six lines)' no
curl_youtube '6b. YouTube without proxy (HTTP result)' no

echo '===== 7. Recent LiveMonitor proxy/recording journal lines ====='
journalctl -u livemonitor --utc --since '30 min ago' --no-pager | grep -iE 'proxy|503|recording|fallback' | tail -40 || echo 'journal query failed or no matching lines'
REMOTE
