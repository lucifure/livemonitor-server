#!/usr/bin/env bash
set -uo pipefail

LM_HOST="${LM_HOST:-ubuntu@100.87.137.48}"
LM_KEY="${LM_KEY:-$HOME/.ssh/oracle.key}"
SSH_OPTS=(-i "$LM_KEY" -o ConnectTimeout=15 -o ServerAliveInterval=20)
RESULT_FILE="$HOME/storage/shared/Documents/$(basename "$0")-RESULT.txt"
mkdir -p "$(dirname "$RESULT_FILE")"
exec > >(tee "$RESULT_FILE") 2>&1

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <local cookies file>"
    exit 2
fi
COOKIE_FILE=$1
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ ! -f "$COOKIE_FILE" ]]; then
    echo "ERROR: cookies file not found: $COOKIE_FILE"
    exit 1
fi
if ! python3 "$SCRIPT_DIR/check_cookies_file.py" "$COOKIE_FILE"; then
    echo "ERROR: cookies validation failed; nothing was copied"
    exit 1
fi

REMOTE_COOKIE_PATH=/home/ubuntu/livemonitor-server/cookies.txt
if ! scp -q "${SSH_OPTS[@]}" "$COOKIE_FILE" "$LM_HOST:$REMOTE_COOKIE_PATH"; then
    echo "ERROR: could not copy cookies file to the VM"
    exit 1
fi
if ! ssh "${SSH_OPTS[@]}" "$LM_HOST" "chmod 600 '$REMOTE_COOKIE_PATH'"; then
    echo "ERROR: cookies file was copied but could not be restricted to mode 600"
    exit 1
fi

ssh "${SSH_OPTS[@]}" "$LM_HOST" 'python3 -' <<'PY'
import datetime
import json
import os
import shutil
import stat
import tempfile

project = "/home/ubuntu/livemonitor-server"
config_path = os.path.join(project, "config.json")
with open(config_path, encoding="utf-8") as config_file:
    config = json.load(config_file)
if not isinstance(config, dict):
    raise SystemExit("ERROR: config.json must contain a JSON object; it was not changed")

backup_path = config_path + ".bak-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
shutil.copy2(config_path, backup_path)
config["cookies_file"] = "/home/ubuntu/livemonitor-server/cookies.txt"
fd, temp_path = tempfile.mkstemp(prefix=".config.json.", suffix=".tmp", dir=project)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as output_file:
        json.dump(config, output_file, indent=2)
        output_file.write("\n")
        output_file.flush()
        os.fsync(output_file.fileno())
    os.chmod(temp_path, stat.S_IMODE(os.stat(config_path).st_mode))
    os.replace(temp_path, config_path)
except Exception:
    try:
        os.unlink(temp_path)
    except FileNotFoundError:
        pass
    raise

def mask(value):
    if isinstance(value, dict):
        return {key: ("***" if any(part in str(key).lower()
                                   for part in ("token", "secret", "key", "pass"))
                      else mask(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [mask(item) for item in value]
    return value

print("cookies installed; run deploy/vm-restart-if-idle.sh to apply")
print("Masked config.json:")
print(json.dumps(mask(config), indent=2, sort_keys=True))
PY
