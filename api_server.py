import hashlib, hmac, json, os, queue, re, shutil, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

VALID_QUALITIES = ("360p", "480p", "720p", "1080p", "best")
CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def log_level(line):
    text = str(line).lower()
    if any(term in text for term in ("error", "failed", "could not", "exited with code", "no output file", "traceback")):
        return "ERROR"
    if any(term in text for term in ("waiting", "backoff", "skipped", "only a video part",
                                    "only an audio part", "low disk", "under 120s")):
        return "WARNING"
    if any(term in text for term in ("recording saved", "finalized", "merged")):
        return "SUCCESS"
    return "INFO"


def start_api(config, ctx):
    token = config.get("api_token", "")
    if not token or token == "CHANGE_ME":
        print("API disabled: api_token is empty or still set to CHANGE_ME in config.json", flush=True)
        return None
    output_dir = config.get("output_dir", os.path.expanduser("~/recordings"))
    bind = config.get("api_bind", "127.0.0.1")
    port = int(config.get("api_port", 8787))
    expected = ("Bearer " + token).encode()

    def in_progress(name):
        return any(name.startswith(p + ".") for p in ctx["active_prefixes"]())

    output_root = os.path.realpath(output_dir)
    thumbs_dir = config.get("thumbs_dir", os.path.expanduser("~/livemonitor-server/thumbs"))
    if os.path.commonpath((output_root, os.path.realpath(thumbs_dir))) == output_root:
        thumbs_dir = os.path.join(os.path.dirname(output_root), "thumbs")
    video_extensions = (".mp4", ".mkv", ".ts")
    duration_cache = {}
    cache_lock = threading.Lock()
    pending = set()
    work_queue = queue.Queue()

    def cache_key(name, size, modified):
        return (name, size, modified)

    def thumbnail_path(name, size, modified):
        digest = hashlib.sha1(f"{name}|{size}|{modified}".encode()).hexdigest()
        return os.path.join(thumbs_dir, digest + ".jpg")

    def enqueue(name, path, size, modified):
        key = cache_key(name, size, modified)
        with cache_lock:
            if key not in duration_cache and key not in pending:
                pending.add(key)
                work_queue.put((key, path, name))

    def media_worker():
        while True:
            key, fpath, name = work_queue.get()
            try:
                # Listings observed while any recording was active are not
                # probed; the next listing can enqueue them once recordings stop.
                if in_progress(name) or ctx["active_prefixes"]():
                    continue
                try:
                    result = subprocess.run(
                        ["nice", "-n", "19", "ffprobe", "-v", "error", "-show_entries",
                         "format=duration", "-of", "csv=p=0", fpath],
                        capture_output=True, text=True, timeout=30, check=True)
                    seconds = int(float(result.stdout.strip()))
                    duration = max(0, seconds)
                except Exception as error:
                    duration = None
                    ctx["log"](f"ffprobe failed for {name}: {error}")
                with cache_lock:
                    duration_cache[key] = duration
                thumb = thumbnail_path(*key)
                try:
                    os.makedirs(thumbs_dir, exist_ok=True)
                    timestamp = 0 if duration is None or duration < 10 else min(60, duration * 0.1)
                    result = subprocess.run(
                        ["nice", "-n", "19", "ffmpeg", "-v", "error", "-y", "-ss", str(timestamp),
                         "-i", fpath, "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", thumb],
                        capture_output=True, text=True, timeout=60, check=True)
                    if not os.path.isfile(thumb) or os.path.getsize(thumb) <= 0:
                        raise RuntimeError("ffmpeg produced no thumbnail")
                    files = sorted((os.path.join(thumbs_dir, f) for f in os.listdir(thumbs_dir)
                                    if f.endswith(".jpg") and os.path.isfile(os.path.join(thumbs_dir, f))),
                                   key=os.path.getmtime, reverse=True)
                    for old in files[300:]:
                        try: os.remove(old)
                        except OSError: pass
                except Exception as error:
                    try:
                        if os.path.exists(thumb): os.remove(thumb)
                    except OSError: pass
                    ctx["log"](f"thumbnail failed for {name}: {error}")
            finally:
                with cache_lock:
                    pending.discard(key)
                work_queue.task_done()

    threading.Thread(target=media_worker, daemon=True).start()

    def safe_path(raw_name):
        name = unquote(raw_name)
        if not name or name in (".", "..") or name != os.path.basename(name):
            return None, None
        path = os.path.join(output_dir, name)
        # A recording must be a real file within output_dir, not a symlink
        # pointing somewhere else on the VM.
        if not os.path.isfile(path) or os.path.islink(path) or os.path.commonpath((output_root, os.path.realpath(path))) != output_root:
            return None, None
        return name, path

    def safe_channel_name(raw_name):
        name = unquote(raw_name)
        if not CHANNEL_NAME_RE.fullmatch(name):
            return None
        return name

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def send_json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_json_body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0 or length > 65536:
                return None, "empty or oversized body"
            raw = self.rfile.read(length)
            try:
                return json.loads(raw), None
            except json.JSONDecodeError:
                return None, "invalid JSON"

        def authorized(self):
            given = self.headers.get("Authorization", "").encode()
            return hmac.compare_digest(given, expected)

        def do_GET(self):
            if not self.authorized():
                return self.send_json(401, {"error": "unauthorized"})
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/status":
                data = ctx["status"]()
                try:
                    du = shutil.disk_usage(output_dir)
                    data["disk"] = {"total": du.total, "used": du.used, "free": du.free}
                except OSError:
                    data["disk"] = None
                return self.send_json(200, data)
            if path == "/channels":
                return self.send_json(200, {"channels": ctx["status"]()["channels"]})
            if path == "/logs":
                qs = parse_qs(parsed.query)
                raw_n = qs.get("limit", qs.get("n", ["200"]))[0]
                try:
                    n = int(raw_n)
                except ValueError:
                    n = 200
                n = max(1, min(n, 2000))
                lines = ctx["logs"](n)
                offset = time.localtime().tm_gmtoff
                return self.send_json(200, {
                    "lines": lines,
                    "levels": [log_level(line) for line in lines],
                    "utc_offset_seconds": offset,
                })
            if path == "/recordings":
                items = []
                if os.path.isdir(output_dir):
                    for entry in os.scandir(output_dir):
                        if entry.is_file():
                            if entry.is_symlink():
                                continue
                            st = entry.stat()
                            progress = in_progress(entry.name)
                            key = cache_key(entry.name, st.st_size, int(st.st_mtime))
                            duration = None
                            if entry.name.lower().endswith(video_extensions) and not progress:
                                with cache_lock:
                                    duration = duration_cache.get(key)
                                    cached = key in duration_cache
                                if not cached:
                                    # Any active recording defers all media work until a later listing.
                                    if not ctx["active_prefixes"]():
                                        enqueue(entry.name, entry.path, st.st_size, int(st.st_mtime))
                            items.append({
                                "name": entry.name,
                                "size": st.st_size,
                                "modified": int(st.st_mtime),
                                "in_progress": progress,
                                "duration": duration,
                            })
                items.sort(key=lambda x: x["modified"], reverse=True)
                return self.send_json(200, {"recordings": items})
            if path.startswith("/recordings/"):
                return self.download(path[len("/recordings/"):])
            if path.startswith("/thumbnails/"):
                return self.thumbnail(path[len("/thumbnails/"):])
            return self.send_json(404, {"error": "not found"})

        def download(self, raw_name):
            name, fpath = safe_path(raw_name)
            if not name:
                return self.send_json(404, {"error": "not found"})
            if in_progress(name):
                return self.send_json(409, {"error": "recording in progress"})
            size = os.path.getsize(fpath)
            start, end, status = 0, size - 1, 200
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                a, _, b = rng[6:].split(",")[0].strip().partition("-")
                try:
                    if a == "":
                        start = max(0, size - int(b))
                    else:
                        start = int(a)
                        if b:
                            end = min(int(b), size - 1)
                except ValueError:
                    return self.send_json(416, {"error": "bad range"})
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            try:
                with open(fpath, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def thumbnail(self, raw_name):
            name, fpath = safe_path(raw_name)
            if not name or in_progress(name) or not name.lower().endswith(video_extensions):
                return self.send_json(404, {"error": "not found"})
            st = os.stat(fpath)
            thumb = thumbnail_path(name, st.st_size, int(st.st_mtime))
            if not os.path.isfile(thumb) or os.path.getsize(thumb) <= 0:
                return self.send_json(404, {"error": "not found"})
            size = os.path.getsize(thumb)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(thumb, "rb") as image:
                shutil.copyfileobj(image, self.wfile)

        def do_POST(self):
            if not self.authorized():
                return self.send_json(401, {"error": "unauthorized"})
            path = urlparse(self.path).path
            if path == "/channels":
                body, err = self.read_json_body()
                if err:
                    return self.send_json(400, {"error": err})
                name = (body or {}).get("name", "")
                url = (body or {}).get("url", "")
                quality = (body or {}).get("quality", "480p")
                if not isinstance(name, str) or not CHANNEL_NAME_RE.fullmatch(name):
                    return self.send_json(400, {"error": "name must be non-empty, letters/digits/_/- only"})
                parsed_url = urlparse(url) if isinstance(url, str) else None
                if not parsed_url or parsed_url.scheme != "https" or parsed_url.hostname not in ("youtube.com", "www.youtube.com"):
                    return self.send_json(400, {"error": "url must be a youtube.com URL"})
                if quality not in VALID_QUALITIES:
                    return self.send_json(400, {"error": f"quality must be one of {VALID_QUALITIES}"})
                ok, err = ctx["add_channel"](name, url, quality)
                if not ok:
                    return self.send_json(409, {"error": err})
                return self.send_json(201, {"added": name})

            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "channels" and parts[2] in ("stop", "start", "resume"):
                name = safe_channel_name(parts[1])
                if not name:
                    return self.send_json(404, {"error": "channel not found"})
                action = parts[2]
                if action == "stop":
                    ok = ctx["pause_channel"](name)
                else:
                    ok = ctx["resume_channel"](name)
                if not ok:
                    return self.send_json(404, {"error": "channel not found"})
                return self.send_json(200, {"channel": name, "action": action})
            return self.send_json(404, {"error": "not found"})

        def do_DELETE(self):
            if not self.authorized():
                return self.send_json(401, {"error": "unauthorized"})
            path = urlparse(self.path).path
            if path == "/logs":
                return self.send_json(200, {"cleared": ctx["clear_logs"]()})
            if path.startswith("/recordings/"):
                name, fpath = safe_path(path[len("/recordings/"):])
                if not name:
                    return self.send_json(404, {"error": "not found"})
                if in_progress(name):
                    return self.send_json(409, {"error": "recording in progress"})
                st = os.stat(fpath)
                try:
                    os.remove(fpath)
                except OSError as e:
                    return self.send_json(500, {"error": str(e)})
                try:
                    thumb = thumbnail_path(name, st.st_size, int(st.st_mtime))
                    if os.path.exists(thumb): os.remove(thumb)
                except OSError:
                    pass
                ctx["log"](f"API: deleted recording {name}")
                return self.send_json(200, {"deleted": name})
            if path.startswith("/channels/"):
                name = safe_channel_name(path[len("/channels/"):])
                if not name:
                    return self.send_json(404, {"error": "channel not found"})
                ok, err = ctx["remove_channel"](name)
                if not ok:
                    return self.send_json(409 if err == "recording" else 404, {"error": err})
                return self.send_json(200, {"removed": name})
            return self.send_json(404, {"error": "not found"})

    # Tailscale can be unavailable at boot.  Binding in this daemon keeps the
    # recorder alive and retries until its configured address is usable.
    def serve_when_available():
        while True:
            try:
                server = ThreadingHTTPServer((bind, port), Handler)
                server.daemon_threads = True
                print(f"API listening on {bind}:{port}", flush=True)
                server.serve_forever()
                return
            except OSError as error:
                print(f"API bind failed on {bind}:{port}; retrying in 5 seconds: {error}", flush=True)
                time.sleep(5)

    # Bind synchronously when possible so callers can immediately use the
    # server (and tests can close it); fall back to the retry thread on boot.
    try:
        server = ThreadingHTTPServer((bind, port), Handler)
    except OSError:
        thread = threading.Thread(target=serve_when_available, daemon=True)
        thread.start()
        return thread
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"API listening on {bind}:{port}", flush=True)
    return server
