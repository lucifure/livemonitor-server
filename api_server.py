import hmac, json, os, re, shutil, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

VALID_QUALITIES = ("360p", "480p", "720p", "1080p", "best")
CHANNEL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def start_api(config, ctx):
    token = config.get("api_token", "")
    if not token:
        print("API disabled: no api_token in config.json", flush=True)
        return None
    output_dir = config.get("output_dir", os.path.expanduser("~/recordings"))
    bind = config.get("api_bind", "127.0.0.1")
    port = int(config.get("api_port", 8787))
    expected = ("Bearer " + token).encode()

    def in_progress(name):
        return any(name.startswith(p + ".") for p in ctx["active_prefixes"]())

    output_root = os.path.realpath(output_dir)

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
                raw_n = qs.get("limit", qs.get("n", ["100"]))[0]
                try:
                    n = int(raw_n)
                except ValueError:
                    n = 100
                n = max(1, min(n, 500))
                return self.send_json(200, {"lines": ctx["logs"](n)})
            if path == "/recordings":
                items = []
                if os.path.isdir(output_dir):
                    for entry in os.scandir(output_dir):
                        if entry.is_file():
                            if entry.is_symlink():
                                continue
                            st = entry.stat()
                            items.append({
                                "name": entry.name,
                                "size": st.st_size,
                                "modified": int(st.st_mtime),
                                "in_progress": in_progress(entry.name),
                            })
                items.sort(key=lambda x: x["modified"], reverse=True)
                return self.send_json(200, {"recordings": items})
            if path.startswith("/recordings/"):
                return self.download(path[len("/recordings/"):])
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
            if path.startswith("/recordings/"):
                name, fpath = safe_path(path[len("/recordings/"):])
                if not name:
                    return self.send_json(404, {"error": "not found"})
                if in_progress(name):
                    return self.send_json(409, {"error": "recording in progress"})
                try:
                    os.remove(fpath)
                except OSError as e:
                    return self.send_json(500, {"error": str(e)})
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
