#!/usr/bin/env python3
import json, os, re, shutil, subprocess, threading, time
from collections import deque
from datetime import datetime

import api_server

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")
# deploy/setup.sh installs yt-dlp into this venv and ffmpeg with apt.  Keep
# fallbacks so `python3 livemonitor_server.py` also remains useful outside it.
YTDLP_BIN = (os.path.join(PROJECT_DIR, ".venv", "bin", "yt-dlp")
             if os.path.isfile(os.path.join(PROJECT_DIR, ".venv", "bin", "yt-dlp"))
             else (shutil.which("yt-dlp") or os.path.expanduser("~/.local/bin/yt-dlp")))
FFMPEG_BIN = "/usr/bin/ffmpeg" if os.path.isfile("/usr/bin/ffmpeg") else (shutil.which("ffmpeg") or "ffmpeg")
LOG_BUFFER = deque(maxlen=500)
STATE = {}
STATE_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()
CONTROL = {}
STARTED_AT = int(time.time())
GLOBALS = {}


def server_version():
    """Return the checked-out git revision without making startup depend on git."""
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=os.path.dirname(CONFIG_PATH), text=True,
                                       stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def save_channels_to_config(channels_list):
    with CONFIG_LOCK:
        cfg = load_config()
        cfg["channels"] = channels_list
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)


def log(channel_name, message):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{channel_name}] {message}"
    LOG_BUFFER.append(line)
    print(line, flush=True)


def set_state(name, **kwargs):
    with STATE_LOCK:
        STATE.setdefault(name, {}).update(kwargs)


def get_status():
    with STATE_LOCK:
        channels = [dict(v, name=k) for k, v in STATE.items()]
    for c in channels:
        c.pop("base_name", None)
        # Android displays JSON null literally.  These fields are always text.
        for field in ("title", "last_error", "video_id"):
            c[field] = c.get(field) or ""
    return {"server_time": int(time.time()), "started_at": STARTED_AT,
            "version": GLOBALS.get("version", "unknown"), "channels": channels}


def active_prefixes():
    with STATE_LOCK:
        return [s["base_name"] for s in STATE.values()
                if s.get("base_name") and s.get("status") in ("recording", "finalizing")]


def sanitize_filename(name, max_len=100):
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name[:max_len] if name else "Live_Stream"


def format_selector(quality):
    quality = (quality or "480p").lower()
    if quality == "best":
        return "bestvideo+bestaudio/best"
    heights = {"360p": 360, "480p": 480, "720p": 720, "1080p": 1080}
    h = heights.get(quality, 480)
    return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"


def extract_balanced_json(html, marker):
    idx = html.find(marker)
    if idx == -1:
        return None
    start = html.find("{", idx)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(html)):
        ch = html[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def check_live(channel_url, proxy, channel_name):
    import requests
    def request(using_proxy):
        options = dict(request_options)
        if using_proxy:
            options["proxies"] = {"http": proxy, "https": proxy}
        return requests.get(channel_url, **options)

    try:
        request_options = {
            "headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            "timeout": 20,
        }
        resp = request(bool(proxy))
    except Exception as e:
        # A configured proxy gets exactly one direct retry.  This is useful
        # when the local proxy has restarted, but avoids hiding other errors.
        if proxy and ("proxy" in str(e).lower() or isinstance(e, requests.exceptions.ProxyError)):
            log(channel_name, f"live-check via proxy failed: {e}; retrying directly")
            try:
                resp = request(False)
                log(channel_name, "live-check succeeded directly after proxy failure")
            except Exception as direct_error:
                log(channel_name, f"live-check direct retry failed: {direct_error}")
                set_state(channel_name, last_error=f"request failed: {direct_error}")
                return None
        else:
            log(channel_name, f"live-check request failed: {e}")
            set_state(channel_name, last_error=f"request failed: {e}")
            return None

    if resp.status_code != 200:
        log(channel_name, f"live-check got HTTP {resp.status_code}")
        set_state(channel_name, last_error=f"HTTP {resp.status_code}",
                  backoff_seconds=300 if resp.status_code == 429 else 0)
        return None

    data = extract_balanced_json(resp.text, "ytInitialPlayerResponse")
    if not data:
        log(channel_name, "unexpected page: no player data (possibly consent or bot-check)")
        set_state(channel_name, last_error="unexpected page: no player data", backoff_seconds=300)
        return None

    video_details = data.get("videoDetails") or {}
    playability = data.get("playabilityStatus") or {}
    streaming_data = data.get("streamingData") or {}

    video_id = video_details.get("videoId", "")
    if not video_id:
        log(channel_name, "unexpected page: player data has no video id")
        set_state(channel_name, last_error="unexpected page: no video id", backoff_seconds=300)
        return None

    status_ok = playability.get("status", "") == "OK"
    is_live_content = bool(video_details.get("isLiveContent"))
    has_playable = bool(
        streaming_data.get("hlsManifestUrl")
        or streaming_data.get("formats")
        or streaming_data.get("adaptiveFormats")
    )

    if not (status_ok and is_live_content and has_playable):
        # This is a successful non-live check, not an error.
        set_state(channel_name, last_error=None, backoff_seconds=0)
        return None

    set_state(channel_name, last_error=None, backoff_seconds=0)
    return {"video_id": video_id, "title": video_details.get("title", video_id)}


def record_stream(channel_name, video_id, title, proxy, output_dir, quality):
    os.makedirs(output_dir, exist_ok=True)
    required = int(GLOBALS.get("min_free_gb", 3)) * 1024 ** 3
    if shutil.disk_usage(output_dir).free < required:
        message = f"not enough free disk space (need {GLOBALS.get('min_free_gb', 3)} GB)"
        log(channel_name, message)
        set_state(channel_name, last_error=message)
        return False
    now = datetime.now()
    base_name = sanitize_filename(title) + "_" + now.strftime("%d%m%Y") + "_" + now.strftime("%H-%M-%S")
    output_template = os.path.join(output_dir, base_name + ".%(ext)s")
    video_url = f"https://youtube.com/watch?v={video_id}"

    cmd = [
        YTDLP_BIN, "--force-ipv4", "--no-update",
        "-f", format_selector(quality),
        "--hls-use-mpegts", "--hls-prefer-native",
        "--skip-unavailable-fragments", "--fragment-retries", "3",
        "--live-from-start",
        "--retries", "3", "--extractor-retries", "3", "--file-access-retries", "3",
        "--retry-sleep", "5", "--socket-timeout", "10",
        "--no-part", "--force-overwrites",
        "-o", output_template, video_url,
    ]
    if proxy:
        cmd[1:1] = ["--proxy", proxy]

    set_state(channel_name, status="recording", video_id=video_id, title=title,
              recording_started_at=int(time.time()), base_name=base_name,
              last_recording_duration_seconds=0)
    started = time.monotonic()
    succeeded = False
    try:
        log(channel_name, f"Recording started: {video_url} -> {output_template}")
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
        duration = max(0, int(time.monotonic() - started))
        set_state(channel_name, last_recording_duration_seconds=duration)
        log(channel_name, f"yt-dlp exited with code {result.returncode}")
        if result.returncode:
            tail = "\n".join((result.stdout or "").splitlines()[-10:])
            log(channel_name, f"yt-dlp failed; output tail: {tail or '(no output)'}")
            set_state(channel_name, last_error=f"yt-dlp exited with code {result.returncode}")
            cleanup_empty_recording_files(output_dir, base_name)
            return False

        set_state(channel_name, status="finalizing")
        produced = None
        for fname in os.listdir(output_dir):
            if fname.startswith(base_name + "."):
                produced = os.path.join(output_dir, fname)
                break

        if not produced:
            tail = "\n".join((result.stdout or "").splitlines()[-10:])
            log(channel_name, f"No output file found; yt-dlp output tail: {tail or '(no output)'}")
            set_state(channel_name, last_error="yt-dlp produced no output file")
            return False

        if produced.endswith(".ts"):
            mp4_path = produced[:-3] + "mp4"
            log(channel_name, f"Converting .ts to mp4: {mp4_path}")
            try:
                conversion = subprocess.run([FFMPEG_BIN, "-y", "-i", produced, "-c", "copy", mp4_path])
                if conversion.returncode != 0 or not os.path.isfile(mp4_path) or os.path.getsize(mp4_path) == 0:
                    message = "ffmpeg conversion failed; keeping original .ts recording"
                    log(channel_name, message)
                    set_state(channel_name, last_error=message)
                    cleanup_empty_recording_files(output_dir, base_name)
                    return False
                os.remove(produced)
            except OSError as error:
                message = f"ffmpeg conversion failed; keeping original .ts recording: {error}"
                log(channel_name, message)
                set_state(channel_name, last_error=message)
                cleanup_empty_recording_files(output_dir, base_name)
                return False
            log(channel_name, f"Recording saved: {mp4_path}")
        else:
            log(channel_name, f"Recording saved (native container, no remux needed): {produced}")
        if duration < 120:
            message = f"recording ended after {duration}s (under 120s)"
            log(channel_name, message)
            set_state(channel_name, last_error=message)
            cleanup_empty_recording_files(output_dir, base_name)
        succeeded = True
        return True
    finally:
        if not succeeded:
            cleanup_empty_recording_files(output_dir, base_name)
        set_state(channel_name, status="idle", video_id=None, title=None,
                  recording_started_at=None, base_name=None)


def cleanup_empty_recording_files(output_dir, base_name):
    """Remove only empty leftovers from this yt-dlp output name."""
    try:
        for fname in os.listdir(output_dir):
            if fname.startswith(base_name + "."):
                path = os.path.join(output_dir, fname)
                if os.path.isfile(path) and os.path.getsize(path) == 0:
                    os.remove(path)
                    log("server", f"Removed empty recording leftover: {fname}")
    except OSError as error:
        log("server", f"Could not clean empty recording leftovers for {base_name}: {error}")


def monitor_channel(channel):
    name, url = channel["name"], channel["url"]
    quality = channel.get("quality", "480p")
    proxy = GLOBALS["proxy"]
    output_dir = GLOBALS["output_dir"]
    poll_interval = GLOBALS["poll_interval"]
    ctrl = CONTROL[name]
    consecutive_failures = 0

    set_state(name, url=url, quality=quality, status="paused" if ctrl["pause"].is_set() else "idle", video_id=None, title=None,
              recording_started_at=None, last_check=None, last_error=None, base_name=None)
    log(name, f"Monitoring started for {url} (quality={quality})")
    while True:
        if ctrl["remove"].is_set():
            log(name, "Monitoring stopped (channel removed)")
            with STATE_LOCK:
                STATE.pop(name, None)
            return
        if ctrl["pause"].is_set():
            set_state(name, status="paused")
            time.sleep(min(poll_interval, 5))
            continue
        delay = poll_interval
        try:
            live_info = check_live(url, proxy, name)
            set_state(name, last_check=int(time.time()))
            if live_info:
                log(name, f"LIVE detected: {live_info['title']} ({live_info['video_id']})")
                result = record_stream(name, live_info["video_id"], live_info["title"], proxy, output_dir, quality)
                duration = STATE.get(name, {}).get("last_recording_duration_seconds", 0)
                if not result or duration < 120:
                    consecutive_failures += 1
                    delay = min(30 * (2 ** (consecutive_failures - 1)), 300)
                    log(name, f"attempt failed ({consecutive_failures} in a row); waiting {delay}s before checking again")
                else:
                    consecutive_failures = 0
                    delay = 5
        except Exception as e:
            log(name, f"monitor loop error: {e}")
            set_state(name, last_error=f"monitor loop error: {e}")
        if delay == poll_interval:
            with STATE_LOCK:
                delay = STATE.get(name, {}).get("backoff_seconds", 0) or poll_interval
        for _ in range(delay):
            if ctrl["remove"].is_set() or ctrl["pause"].is_set():
                break
            time.sleep(1)


def spawn_channel_thread(channel):
    name = channel["name"]
    CONTROL[name] = {"pause": threading.Event(), "remove": threading.Event()}
    if channel.get("paused"):
        CONTROL[name]["pause"].set()
    t = threading.Thread(target=monitor_channel, args=(channel,), daemon=True)
    t.start()
    return t


def add_channel(name, url, quality):
    with STATE_LOCK:
        exists = name in STATE
    if exists or name in CONTROL:
        return False, "channel already exists"
    cfg = load_config()
    channels = cfg.get("channels", [])
    if any(c["name"] == name for c in channels):
        return False, "channel already exists"
    new_channel = {"name": name, "url": url, "quality": quality}
    channels.append(new_channel)
    save_channels_to_config(channels)
    spawn_channel_thread(new_channel)
    log(name, f"Channel added via API: {url} (quality={quality})")
    return True, None


def remove_channel(name):
    if name not in CONTROL:
        return False, "channel not found"
    with STATE_LOCK:
        st = STATE.get(name, {})
        if st.get("status") in ("recording", "finalizing"):
            return False, "recording"
    CONTROL[name]["remove"].set()
    cfg = load_config()
    channels = [c for c in cfg.get("channels", []) if c["name"] != name]
    save_channels_to_config(channels)
    CONTROL.pop(name, None)
    return True, None


def pause_channel(name):
    if name not in CONTROL:
        return False
    CONTROL[name]["pause"].set()
    _set_channel_paused_in_config(name, True)
    log(name, "Paused via API")
    return True


def resume_channel(name):
    if name not in CONTROL:
        return False
    CONTROL[name]["pause"].clear()
    _set_channel_paused_in_config(name, False)
    log(name, "Resumed via API")
    return True


def _set_channel_paused_in_config(name, paused):
    cfg = load_config()
    channels = cfg.get("channels", [])
    for channel in channels:
        if channel.get("name") == name:
            channel["paused"] = paused
            break
    save_channels_to_config(channels)


def main():
    config = load_config()
    GLOBALS["proxy"] = config.get("proxy", "")
    GLOBALS["output_dir"] = config.get("output_dir", os.path.expanduser("~/recordings"))
    GLOBALS["poll_interval"] = int(config.get("poll_interval_seconds", 60))
    GLOBALS["min_free_gb"] = config.get("min_free_gb", 3)
    GLOBALS["version"] = server_version()
    log("server", f"LiveMonitor server version {GLOBALS['version']}")
    channels = config.get("channels", [])
    if not channels:
        print("No channels configured in config.json.")

    api_server.start_api(config, {
        "status": get_status,
        "logs": lambda n: list(LOG_BUFFER)[-n:],
        "active_prefixes": active_prefixes,
        "log": lambda msg: log("api", msg),
        "add_channel": add_channel,
        "remove_channel": remove_channel,
        "pause_channel": pause_channel,
        "resume_channel": resume_channel,
    })

    threads = [spawn_channel_thread(c) for c in channels]
    for t in threads:
        t.join()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
