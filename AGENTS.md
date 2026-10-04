# LiveMonitor server: standing instructions

## What this is
A Python server on one Oracle Cloud Ubuntu VM. It watches YouTube channels, records live streams with yt-dlp + ffmpeg, and serves a small authenticated HTTP API used by an Android app (repo lucifure/LiveMonitorChaquopy). The owner is not a programmer: prefer simple code, plain-English docs, and loud, clear logging over cleverness.

## Hard rules
1. You cannot reach the VM or YouTube. Never ask for credentials. Test with mocks (requests, subprocess, time).
2. Never commit secrets. config.json, tokens, keys, recordings and .venv are gitignored. config.example.json holds fake values only.
3. Do not break the Android app. JSON field names, types and status codes returned today by api_server.py are the contract. Pin them with tests BEFORE changing any behaviour.
4. Keep `python3 livemonitor_server.py` working as the entry point unless the service file and docs are updated in the same change.
5. Minimal dependencies, pinned in requirements.txt. Support Python 3.10 or newer.
6. Small, focused commits. Every PR description says in plain English: what changed, what was tested, and what could NOT be verified without the real VM.
7. `python -m pytest` must pass before you finish.

## Environment on the VM
- Ubuntu, user `ubuntu`, project dir /home/ubuntu/livemonitor-server, systemd unit `livemonitor`, recordings in the folder set by config `output_dir`.
- config.json keys: proxy (empty = direct connection), output_dir, poll_interval_seconds, channels [{name, url, quality}], api_port, api_token, api_bind.
- The API listens on the Tailscale address in `api_bind`. Tailscale may not be up yet at boot, so the service must retry binding, never die permanently.
- Updates are deployed by a script that lives OUTSIDE the repo (~/deploy-livemonitor.sh). It does: git pull, `python3 -m py_compile *.py`, `sudo systemctl restart livemonitor`, then expects GET /status with the bearer token to return 200 within ~20 seconds, otherwise it rolls back. Keep this working: do not rename the unit, keep startup fast.

## API the app uses (Bearer token on every request; wrong or missing token gives 401 {"error": "unauthorized"})
The responses of api_server.py today are the contract. The app uses at least:
| Request | Notes |
|---|---|
| GET /status | {channels:[{name,url,quality,status,title,last_error,...}], disk:{free,total}}. Status values: idle, recording, finalizing, paused |
| GET /recordings | {recordings:[{name,size,modified,in_progress}]} |
| GET /recordings/<name> | file bytes, no redirects |
| DELETE /recordings/<name> | 409 if it is being recorded, 404 if missing |
| POST /channels | body {name,url,quality}, 400 if invalid |
| POST /channels/<name>/resume, /stop (and /start if it exists today) | keep current behaviour |
| DELETE /channels/<name> | removes the channel, keeps recordings |
| GET /logs?limit=N | {lines:[...]} |

## Behaviour rules
1. Errors never change a channel's status. Only an explicit user action through the API (stop/pause) sets `paused`. Clear `last_error` after a successful live or normal non-live page check.
2. Classify every HTTP 200 YouTube page: a playable live player response is live; a normal channel/watch page with `ytInitialData` or channel metadata and no block markers is not live; consent redirects, unusual-traffic/bot prompts, and `LOGIN_REQUIRED` are blocked; anything else is an unexpected page. Blocked checks set `last_error` to `YouTube blocked this network (bot or consent check)`, log at most once per 10 minutes, and wait 10 minutes before checking that channel again. Unexpected pages set `last_error` to `unexpected page` and log at most once per hour.
3. If yt-dlp output contains `Sign in to confirm` or `not a bot`, use the same blocked error, log its last five output lines, wait 10 minutes, and keep monitoring. Never set `paused` for an error.
4. A channel the user paused stays paused after a restart. Everything else starts as idle.
5. If a recording ends while the stream is still live, start a new recording after a short delay. If yt-dlp fails or produces no file, log the last lines of its output.
6. Refuse to start a recording when free disk is below config `min_free_gb` (default 3). Set `last_error` and keep checking.
7. Never send JSON null for title, last_error or video_id. Send "" instead (the app prints the word "null").
8. Log the server version (git short hash) at startup and include `version` in /status.
9. Security: constant-time token comparison; validate channel names (^[A-Za-z0-9_-]{1,64}$) and URLs (https only, host youtube.com or www.youtube.com); /recordings endpoints serve only plain file names inside output_dir (no path traversal, no symlink escape); never log the token.
10. If `proxy` is set and a request fails with a proxy error, retry once directly and log which path worked.

## Definition of done
Tests pass locally and in CI, and the PR description follows hard rule 6.
