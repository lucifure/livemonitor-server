#!/usr/bin/env python3
import json, os, re, shutil, signal, subprocess, threading, time
from collections import deque
from datetime import datetime

import api_server

log_level = api_server.log_level

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")
# deploy/setup.sh installs yt-dlp into this venv and ffmpeg with apt.  Keep
# fallbacks so `python3 livemonitor_server.py` also remains useful outside it.
YTDLP_BIN = (os.path.join(PROJECT_DIR, ".venv", "bin", "yt-dlp")
             if os.path.isfile(os.path.join(PROJECT_DIR, ".venv", "bin", "yt-dlp"))
             else (shutil.which("yt-dlp") or os.path.expanduser("~/.local/bin/yt-dlp")))
FFMPEG_BIN = "/usr/bin/ffmpeg" if os.path.isfile("/usr/bin/ffmpeg") else (shutil.which("ffmpeg") or "ffmpeg")
FFPROBE_BIN = "/usr/bin/ffprobe" if os.path.isfile("/usr/bin/ffprobe") else (shutil.which("ffprobe") or "ffprobe")
LOG_BUFFER = deque(maxlen=2000)
LOG_LOCK = threading.Lock()
STATE = {}
STATE_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()
CONTROL = {}
STOP_GRACE_SECONDS = 120
STOP_TERM_SECONDS = 15
STARTED_AT = int(time.time())
GLOBALS = {}
BLOCKED_PAGE_CHECKS = {}


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
    with LOG_LOCK:
        LOG_BUFFER.append(line)
    print(line, flush=True)


def get_logs(limit):
    with LOG_LOCK:
        return list(LOG_BUFFER)[-limit:]


def clear_logs():
    with LOG_LOCK:
        removed = len(LOG_BUFFER)
        LOG_BUFFER.clear()
        LOG_BUFFER.append("Server log cleared from the app.")
        return removed


def set_state(name, **kwargs):
    with STATE_LOCK:
        STATE.setdefault(name, {}).update(kwargs)


def get_status():
    with STATE_LOCK:
        channels = [dict(v, name=k) for k, v in STATE.items()]
    for c in channels:
        c.pop("base_name", None)
        c.setdefault("last_check_at", 0.0)
        c.setdefault("last_live_at", 0.0)
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


def extract_player_response(html):
    """Parse only an actual ytInitialPlayerResponse assignment."""
    match = re.search(r"ytInitialPlayerResponse\s*=\s*\{", html)
    if not match:
        return None
    return extract_balanced_json(html[match.start():], "=")


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

    data = extract_player_response(resp.text)
    if not data:
        if (resp.status_code == 200
                and "consent.youtube.com" not in getattr(resp, "url", "")
                and "ytInitialData" in resp.text):
            blocked = BLOCKED_PAGE_CHECKS.pop(channel_name, None)
            if blocked:
                log(channel_name, f"page check recovered after {blocked['count']} blocked checks")
            set_state(channel_name, last_error=None, backoff_seconds=0)
            return None
        _log_blocked_page(channel_name, "unexpected page: no player data (possibly consent or bot-check)")
        set_state(channel_name, last_error="unexpected page: no player data", backoff_seconds=300)
        return None

    video_details = data.get("videoDetails") or {}
    playability = data.get("playabilityStatus") or {}
    streaming_data = data.get("streamingData") or {}

    playability_reason = str(playability.get("reason", ""))
    if (playability.get("status") == "LOGIN_REQUIRED"
            or "not a bot" in playability_reason.lower()):
        _log_blocked_page(channel_name, "blocked by YouTube: sign in to confirm you're not a bot (backoff)")
        set_state(channel_name, last_error="blocked by YouTube (bot check)", backoff_seconds=300)
        return None

    video_id = video_details.get("videoId", "")
    if not video_id:
        _log_blocked_page(channel_name, "unexpected page: player data has no video id")
        set_state(channel_name, last_error="unexpected page: no video id", backoff_seconds=300)
        return None

    blocked = BLOCKED_PAGE_CHECKS.pop(channel_name, None)
    if blocked:
        log(channel_name, f"page check recovered after {blocked['count']} blocked checks")

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


def _log_blocked_page(channel_name, message):
    now = time.time()
    blocked = BLOCKED_PAGE_CHECKS.get(channel_name)
    if blocked is None:
        BLOCKED_PAGE_CHECKS[channel_name] = {"count": 1, "started_at": now, "last_logged_at": now}
        log(channel_name, message)
        return
    blocked["count"] += 1
    if now - blocked["last_logged_at"] >= 3600:
        since = datetime.fromtimestamp(blocked["started_at"]).strftime("%H:%M")
        log(channel_name, f"{message} (still blocked; {blocked['count']} checks since {since})")
        blocked["last_logged_at"] = now


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

    ctrl = CONTROL.get(channel_name, {})
    stop_now = ctrl.setdefault("stop_now", threading.Event())
    stop_now.clear()
    if ctrl.get("pause") and ctrl["pause"].is_set():
        set_state(channel_name, status="paused")
        return True
    set_state(channel_name, status="recording", video_id=video_id, title=title,
              recording_started_at=int(time.time()), last_live_at=float(time.time()), base_name=base_name,
              last_recording_duration_seconds=0)
    started = time.monotonic()
    succeeded = False
    stopped_by_user = False
    try:
        log(channel_name, f"Recording started: {video_url} -> {output_template}")
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", bufsize=1,
                                   start_new_session=True)
        output_lines = deque(maxlen=200)

        reader = threading.Thread(target=read_process_output,
                                  args=(process.stdout, output_lines), daemon=True)
        reader.start()
        while process.poll() is None:
            if stop_now and stop_now.is_set():
                stopped_by_user = True
                set_state(channel_name, status="finalizing")
                log(channel_name, "Stop requested; asking yt-dlp to finish and save")
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    log(channel_name, "yt-dlp did not finish after SIGINT; sending SIGTERM")
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=STOP_TERM_SECONDS)
                    except subprocess.TimeoutExpired:
                        log(channel_name, "yt-dlp did not finish after SIGTERM; sending SIGKILL")
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                break
            time.sleep(1)
        return_code = process.wait()
        reader.join(timeout=2)
        if stop_now.is_set():
            stopped_by_user = True
            set_state(channel_name, status="finalizing")
        duration = max(0, int(time.monotonic() - started))
        set_state(channel_name, last_recording_duration_seconds=duration)
        set_state(channel_name, status="finalizing")
        finalized_path = finalize_recording_output(output_dir, base_name, channel_name)
        cleanup_empty_recording_files(output_dir, base_name)
        if return_code and not stopped_by_user:
            usable_saved_file = (finalized_path is not None and finalized_path is not False
                                 and os.path.isfile(finalized_path)
                                 and os.path.getsize(finalized_path) >= 1024 * 1024
                                 and duration >= 120)
            if usable_saved_file:
                output_tail = [line for line in output_lines if line.strip()][-15:]
                for line in output_tail:
                    log(channel_name, line)
                try:
                    channel_url = STATE.get(channel_name, {}).get("url")
                    live_info = check_live(channel_url, proxy, channel_name) if channel_url else None
                    check_failed = bool(STATE.get(channel_name, {}).get("last_error"))
                except Exception:
                    live_info = None
                    check_failed = True
                if check_failed:
                    summary = f"yt-dlp finished with code {return_code} (normal at stream end; live status unknown); recording saved"
                elif live_info and live_info.get("video_id") == video_id:
                    summary = f"yt-dlp ended early (code {return_code}) while the stream is still live; recording saved, a new recording will start"
                else:
                    summary = f"yt-dlp finished with code {return_code} (normal at stream end); recording saved"
                log(channel_name, summary)
                set_state(channel_name, last_error=None)
            else:
                log(channel_name, f"yt-dlp exited with code {return_code}")
                tail = "\n".join(list(output_lines)[-10:])
                log(channel_name, f"yt-dlp failed; output tail: {tail or '(no output)'}")
                set_state(channel_name, last_error=f"yt-dlp exited with code {return_code}")
                cleanup_empty_recording_files(output_dir, base_name)
                return False
        else:
            log(channel_name, f"yt-dlp exited with code {return_code}")

        if finalized_path is None:
            if stopped_by_user:
                log(channel_name, "Stop requested; nothing was recorded")
                cleanup_empty_recording_files(output_dir, base_name)
                succeeded = True
                return True
            tail = "\n".join(list(output_lines)[-10:])
            log(channel_name, f"No output file found; yt-dlp output tail: {tail or '(no output)'}")
            set_state(channel_name, last_error="yt-dlp produced no output file")
            return False
        if finalized_path is False:
            if stopped_by_user:
                cleanup_empty_recording_files(output_dir, base_name)
                succeeded = True
                return True
            message = "recording output could not be finalized; source files were kept"
            set_state(channel_name, last_error=message)
            cleanup_empty_recording_files(output_dir, base_name)
            return False
        if duration < 120 and not stopped_by_user:
            message = f"recording ended after {duration}s (under 120s)"
            log(channel_name, message)
            set_state(channel_name, last_error=message)
            cleanup_empty_recording_files(output_dir, base_name)
        succeeded = True
        return True
    finally:
        if not succeeded:
            cleanup_empty_recording_files(output_dir, base_name)
        final_status = "paused" if stopped_by_user or (ctrl.get("pause") and ctrl["pause"].is_set()) else "idle"
        set_state(channel_name, status=final_status, video_id=None, title=None,
                  recording_started_at=None, base_name=None)


def finalize_recording_output(output_dir, base_name, channel_name="server"):
    """Merge split yt-dlp parts or finalize one native output. Return path, False, or None."""
    names = sorted(os.listdir(output_dir))
    part_re = re.compile(r"^" + re.escape(base_name) + r"\.f\d+\.[^.]+$")
    part_names = [name for name in names if part_re.fullmatch(name)]
    parts = [os.path.join(output_dir, name) for name in part_names
             if os.path.isfile(os.path.join(output_dir, name))
             and os.path.getsize(os.path.join(output_dir, name)) > 0]

    if not parts:
        for extension in ("mp4", "mkv"):
            final_path = os.path.join(output_dir, f"{base_name}.{extension}")
            if os.path.isfile(final_path) and os.path.getsize(final_path) > 0:
                log(channel_name, f"Recording saved: {final_path}")
                return final_path
        ts_path = os.path.join(output_dir, f"{base_name}.ts")
        if os.path.isfile(ts_path) and os.path.getsize(ts_path) > 0:
            mp4_path = os.path.join(output_dir, f"{base_name}.mp4")
            log(channel_name, f"Converting .ts to mp4: {mp4_path}")
            try:
                result = subprocess.run([FFMPEG_BIN, "-y", "-i", ts_path, "-c", "copy", mp4_path])
                if result.returncode == 0 and os.path.isfile(mp4_path) and os.path.getsize(mp4_path) > 0:
                    os.remove(ts_path)
                    log(channel_name, f"Recording saved: {mp4_path}")
                    return mp4_path
            except OSError as error:
                log(channel_name, f"Could not convert {ts_path}; original was kept: {error}")
            log(channel_name, f"Could not convert {ts_path}; original was kept")
            return False
        final_re = re.compile(r"^" + re.escape(base_name) + r"\.[^.]+$")
        for name in names:
            if not final_re.fullmatch(name):
                continue
            final_path = os.path.join(output_dir, name)
            if os.path.isfile(final_path) and os.path.getsize(final_path) > 0:
                log(channel_name, f"Recording saved (native container, no remux needed): {final_path}")
                return final_path
        return None

    if len(parts) == 1:
        path = parts[0]
        try:
            probe = subprocess.run([FFPROBE_BIN, "-v", "error", "-show_entries",
                                    "stream=codec_type,codec_name", "-of", "json", path],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stream_types = {s.get("codec_type") for s in json.loads(probe.stdout or "{}").get("streams", [])}
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            stream_types = set()
        if "video" in stream_types and "audio" not in stream_types:
            log(channel_name, f"only a video part was found; kept as-is: {os.path.basename(path)}")
        elif "audio" in stream_types and "video" not in stream_types:
            log(channel_name, f"only an audio part was found; kept as-is: {os.path.basename(path)}")
        else:
            log(channel_name, f"Could not finalize part file; kept as-is: {os.path.basename(path)}")
        return False

    probes = []
    for path in parts:
        try:
            probe = subprocess.run([FFPROBE_BIN, "-v", "error", "-show_entries",
                                    "stream=codec_type,codec_name", "-of", "json", path],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if probe.returncode != 0:
                raise ValueError("ffprobe returned an error")
            streams = json.loads(probe.stdout or "{}").get("streams", [])
            probes.append((path, streams))
        except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as error:
            log(channel_name, f"Could not inspect part files {', '.join(os.path.basename(p) for p in parts)}: {error}")
            return False

    video_parts = [(path, next(s for s in streams if s.get("codec_type") == "video"))
                   for path, streams in probes if any(s.get("codec_type") == "video" for s in streams)]
    audio_parts = [(path, next(s for s in streams if s.get("codec_type") == "audio"))
                   for path, streams in probes if any(s.get("codec_type") == "audio" for s in streams)]
    if not video_parts or not audio_parts:
        names_text = ", ".join(os.path.basename(p) for p in parts)
        kind = "video" if video_parts else "audio"
        log(channel_name, f"Only {kind} parts were found in {names_text}; parts were kept")
        return False

    video_path, video_stream = video_parts[0]
    audio_path, audio_stream = audio_parts[0]
    compatible = (
        video_stream.get("codec_name") in ("h264", "avc1")
        and audio_stream.get("codec_name") == "aac"
        and os.path.splitext(video_path)[1].lower() == ".mp4"
        and os.path.splitext(audio_path)[1].lower() in (".mp4", ".m4a")
    )
    extension = "mp4" if compatible else "mkv"
    output_path = os.path.join(output_dir, f"{base_name}.{extension}")
    inputs = [video_path, audio_path]
    base_cmd = [FFMPEG_BIN, "-y", "-i", inputs[0], "-i", inputs[1],
                "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", output_path]
    display_parts = ", ".join(os.path.basename(p) for p in parts)
    for attempt in range(2):
        cmd = base_cmd if attempt == 0 else [FFMPEG_BIN, "-y", "-err_detect", "ignore_err",
                                             "-i", inputs[0], "-err_detect", "ignore_err",
                                             "-i", inputs[1], "-map", "0:v:0", "-map", "1:a:0",
                                             "-c", "copy", output_path]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
                for path in parts:
                    try:
                        os.remove(path)
                    except OSError as error:
                        log(channel_name, f"Merged output saved, but could not remove {os.path.basename(path)}: {error}")
                log(channel_name, f"Recording saved: {output_path} (merged {display_parts})")
                return output_path
        except (OSError, subprocess.SubprocessError) as error:
            log(channel_name, f"Merge attempt {attempt + 1} failed for {display_parts}: {error}")
    log(channel_name, f"Could not merge parts {display_parts}; all part files were kept")
    return False


def recover_recordings(output_dir):
    """Merge old split recordings and remove only old, empty part files."""
    if not os.path.isdir(output_dir):
        log("server", f"Startup recovery skipped; output directory not found: {output_dir}")
        return
    now = time.time()
    prefix_pattern = re.compile(r"^(.*)\.f\d+\.[^.]+$")
    groups = {}
    for name in sorted(os.listdir(output_dir)):
        match = prefix_pattern.fullmatch(name)
        if not match:
            continue
        path = os.path.join(output_dir, name)
        if os.path.isfile(path):
            groups.setdefault(match.group(1), []).append(path)

    for base_name, paths in sorted(groups.items()):
        if not any(os.path.getsize(path) > 0 for path in paths):
            continue
        if base_name in set(active_prefixes()):
            log("server", f"Startup recovery skipped active recording {base_name}")
            continue
        if any(now - os.path.getmtime(path) <= 5 * 60 for path in paths):
            log("server", f"Startup recovery skipped recent part files for {base_name}")
            continue
        result = finalize_recording_output(output_dir, base_name, "server")
        if result is False or result is None:
            log("server", f"Startup recovery could not finalize {base_name}")
        else:
            log("server", f"Startup recovery finalized {base_name}: {os.path.basename(result)}")

    for name in sorted(os.listdir(output_dir)):
        if not prefix_pattern.fullmatch(name):
            continue
        path = os.path.join(output_dir, name)
        if not os.path.isfile(path) or os.path.getsize(path) != 0:
            continue
        base_name = prefix_pattern.fullmatch(name).group(1)
        if base_name in set(active_prefixes()):
            continue
        try:
            if now - os.path.getmtime(path) > 10 * 60:
                os.remove(path)
                log("server", f"Startup recovery removed old empty part file {name}")
        except OSError as error:
            log("server", f"Startup recovery could not remove {name}: {error}")


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


def read_process_output(stream, output_lines):
    """Drain subprocess output continuously while retaining only its last 200 lines."""
    try:
        for line in stream:
            output_lines.append(line.rstrip("\r\n"))
    except (OSError, ValueError):
        pass


def monitor_channel(channel):
    name, url = channel["name"], channel["url"]
    quality = channel.get("quality", "480p")
    proxy = GLOBALS["proxy"]
    output_dir = GLOBALS["output_dir"]
    poll_interval = GLOBALS["poll_interval"]
    ctrl = CONTROL[name]
    consecutive_failures = 0

    set_state(name, url=url, quality=quality, status="paused" if ctrl["pause"].is_set() else "idle", video_id=None, title=None,
              recording_started_at=None, last_check=None, last_check_at=0.0, last_live_at=0.0,
              last_error=None, base_name=None)
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
            checked_at = float(time.time())
            set_state(name, last_check=int(checked_at), last_check_at=checked_at)
            if live_info:
                set_state(name, last_live_at=float(time.time()))
                if ctrl["pause"].is_set():
                    set_state(name, status="paused")
                    continue
                log(name, f"LIVE detected: {live_info['title']} ({live_info['video_id']})")
                result = record_stream(name, live_info["video_id"], live_info["title"], proxy, output_dir, quality)
                duration = STATE.get(name, {}).get("last_recording_duration_seconds", 0)
                if ctrl["pause"].is_set():
                    set_state(name, status="paused")
                elif not result or duration < 120:
                    consecutive_failures += 1
                    delay = min(30 * (2 ** (consecutive_failures - 1)), 300)
                    log(name, f"attempt failed ({consecutive_failures} in a row); waiting {delay}s before checking again")
                else:
                    consecutive_failures = 0
                    delay = 5
            elif STATE.get(name, {}).get("last_error") is None:
                consecutive_failures = 0
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
    CONTROL[name] = {"pause": threading.Event(), "remove": threading.Event(), "stop_now": threading.Event()}
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
    CONTROL[name].setdefault("stop_now", threading.Event()).set()
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
        "logs": get_logs,
        "clear_logs": clear_logs,
        "active_prefixes": active_prefixes,
        "log": lambda msg: log("api", msg),
        "add_channel": add_channel,
        "remove_channel": remove_channel,
        "pause_channel": pause_channel,
        "resume_channel": resume_channel,
    })

    threads = [spawn_channel_thread(c) for c in channels]
    recovery = threading.Thread(target=recover_recordings,
                                args=(GLOBALS["output_dir"],), daemon=True)
    recovery.start()
    for t in threads:
        t.join()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
